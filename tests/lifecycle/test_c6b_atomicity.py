"""C6b - lifecycle Qdrant<->SQLite atomicity red contract (tests-only, ZERO src).

Slice: slice-c6b-lifecycle-qdrant-sqlite-atomicity (Issue #437). Design (accepted direction + Yua's fork
rulings + corrections A-J, 2026-07-13): a durable-intent transactional outbox behind a distinct
`LifecycleTransitionCoordinator` + `LifecycleOutbox` on a shared SQLite events+outbox DB. See
docs/Musubi/13-decisions/c6b-lifecycle-atomicity-design.md for the full state machine + crash matrix.

The contract is 22 behavior-shaped strict-xfail reds (R1-R22) + 3 guards (G1/G2/G3), each labeled:

  Phase-1 source acceptance (flip green with the coordinator/outbox implementation):
    R1  durable PENDING intent committed BEFORE the Qdrant mutation
    R2  SQLite unavailable at begin => Qdrant untouched + Err
    R3  transient Qdrant failure => Ok(Pending(operation_key,event_id)), no FINAL, reconciler completes
    R4  terminal failure => Err/ABANDONED, never a FINAL event
    R5  crash C1 (after PENDING, before Qdrant) => reconcile replays or abandons; no false FINAL
    R6  crash C2 (after Qdrant, before APPLIED) => reconcile readback-confirms -> APPLIED->FINAL
    R7  crash C3 (after APPLIED, before atomic FINAL txn) => reconcile redoes it; exactly one FINAL
    R8  finalize atomicity - FINAL event insert + outbox->FINAL in ONE SQLite transaction
    R9  idempotent replay => replaying a PENDING twice yields one FINAL + one effective apply
    R10 operation_key idempotency across CALLER retries => same logical request twice = one operation
    R11 single active intent per (collection,object_id) => concurrent begins serialize/reuse/reject
    R12 hard expected-version fence => stale expected => Err/abandon; stale replay abandons not clobbers
    R13 conditional apply + full readback (version+state+patch SHA, not version alone)
    R14 hard cap => at pending cap, begin => Err(cap_exceeded), Qdrant untouched
    R15 transient failure NEVER ABANDONED by attempt count => N transient failures keep it PENDING
    R16 reconciliation lease prevents double-processing
    R17 expired-lease reclaim
    R18 no poison-row starvation => one stuck transient row does not block other PENDING rows
    R19 outbox content never in logs/metric labels; row stores minimal patch + SHA, not arbitrary payload
    R20 rollback refuses on any nonterminal row + stops the worker first; terminal-row cleanup exists
    R21 caller outcome is three-way (Final/Pending/Err); Pending carries operation/event id
    R22 two DIFFERENT requested transitions race on one object => the loser cannot mutate/overwrite
    G2  coordinator callsite inventory is exactly the reviewed set
    G3  AST: the three-way TransitionOutcome is consumed, never dropped

  Defect closure (green ONLY when slice-h5-unify-state-mutation migrates every path):
    G1  mechanical AST guard: NO direct `state`-writing `set_payload` outside the coordinator

Tranche 1: **G1** (closure-gate) + its rule-discrimination proof (pure AST over src).
Tranche 2 (in progress): the Phase-1 behavior harness + reds, starting with **R1**. These drive the
future `LifecycleTransitionCoordinator` against an in-memory Qdrant + a real SQLite outbox. Each red is
strict-xfail today because the coordinator is unbuilt, but its decorator names the SPECIFIC defect it
targets and its assertion discriminates that behavior — so a WRONG coordinator leaves its own red
unflipped (proven at red-proof), not a single shared "coordinator absent" reason.

**LOCKED Phase-1 coordinator API** (Yua 2026-07-13):
`LifecycleTransitionCoordinator(client=<qdrant>, db_path=<shared events+outbox sqlite>)` with
`.transition(intent: TransitionIntent) -> Result[TransitionOutcome, TransitionError]`.
- `TransitionIntent` (frozen value object): `collection, object_id, namespace, expected_version`
  (required, no None at the canonical boundary), `target_state, actor, reason`; `operation_key` optional
  (coordinator derives a stable canonical key when absent); minimal deterministic patch fields + patch_sha
  as reds require.
- `TransitionOutcome = TransitionFinal | TransitionPending` (literal discriminator). `TransitionFinal`:
  `operation_key, event_id, TransitionResult`. `TransitionPending`: `operation_key, event_id`, bounded
  non-secret retry metadata. **APPLIED is an INTERNAL outbox state, never a public success outcome.** Err
  means no future mutation will occur.
- Shared DB: ONE `lifecycle_sqlite_path` file holds `lifecycle_events` (C6) + `lifecycle_outbox` (C6b),
  so the FINAL event insert + outbox->FINAL happen in one SQLite transaction. Distinct sink/outbox/
  coordinator types.
- Reconciler: `reconcile_once(*, limit: int = 100) -> ReconcileReport` (constructor owns
  clock/lease/backoff for deterministic tests); report carries bounded counts
  (claimed/finalized/pending/abandoned/failed), no content. The lifecycle-runner calls `reconcile_once`
  at startup + periodically; it embeds no second transition algorithm.
A durable `lifecycle_outbox` row (operation_key PRIMARY KEY, object_id, collection, target_state,
expected_version, state, event_id, ...) is committed BEFORE the Qdrant mutation. Tests inspect that table
+ `lifecycle_events` + Qdrant state directly.

    uv run pytest tests/lifecycle/test_c6b_atomicity.py -v
"""

import ast
import asyncio
import os
import sqlite3
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.types.common import Err, Ok
from musubi.types.episodic import EpisodicMemory

_SRC = Path(__file__).resolve().parents[2] / "src" / "musubi"

#: The canonical relative path (repair 1: NOT a bare basename, which would exempt any coordinator.py
#: anywhere under src/). A `state`-writing transition `set_payload` is permitted ONLY in this module.
_COORDINATOR_REL = "lifecycle/coordinator.py"

#: Repair 3: G1 covers POST-CREATE lifecycle transitions only. Initial-state writes on object creation
#: (e.g. curated `create`) are a separate capture/create-atomicity concern (M9 / a deliberately-approved
#: C6b extension), NOT forced through the transition coordinator. Exclude creation functions.
_CREATE_FUNCS = {"create"}


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _expr_has_state(expr: ast.AST) -> bool:
    """A `state=` keyword or a `"state"` dict key anywhere in this expression subtree."""
    for n in ast.walk(expr):
        if isinstance(n, ast.keyword) and n.arg == "state":
            return True
        if isinstance(n, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "state" for k in n.keys
        ):
            return True
    return False


def _refs_tainted(expr: ast.AST, tainted: set[str]) -> bool:
    return any(isinstance(x, ast.Name) and x.id in tainted for x in ast.walk(expr))


def _tainted_state_names(func: ast.AST) -> set[str]:
    """Backward taint fixpoint (repair 2): the set of local names that carry a `state` write. A name is
    tainted if it is assigned/updated with `state=` (or `["state"]=`), or assigned from an expression
    that references an already-tainted name (e.g. `updated = Model.model_validate(data)` where `data`
    was `data.update(state=...)`). This ties state to a SPECIFIC payload variable via dataflow rather
    than pairing any `set_payload` in the same function with an unrelated state value."""
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for n in ast.walk(func):
            if isinstance(n, ast.Assign):
                for tgt in n.targets:  # X["state"] = ...
                    if (
                        isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Name)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == "state"
                        and tgt.value.id not in tainted
                    ):
                        tainted.add(tgt.value.id)
                        changed = True
                if _expr_has_state(n.value) or _refs_tainted(n.value, tainted):
                    for tgt in n.targets:
                        if isinstance(tgt, ast.Name) and tgt.id not in tainted:
                            tainted.add(tgt.id)
                            changed = True
            if (  # X.update(state=...) / X.update(<tainted>)
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "update"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id not in tainted
                and (_expr_has_state(n) or _refs_tainted(n, tainted))
            ):
                tainted.add(n.func.value.id)
                changed = True
    return tainted


def _payload_arg(call: ast.Call) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "payload":
            return kw.value
    return None


def _root_name(expr: ast.AST) -> str | None:
    """The variable a payload flows from: `X`, or `X.model_dump(...)`."""
    if isinstance(expr, ast.Name):
        return expr.id
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and isinstance(expr.func.value, ast.Name)
    ):
        return expr.func.value.id
    return None


def _call_writes_state(call: ast.Call, tainted: set[str]) -> bool:
    """Does THIS `set_payload` call's payload argument carry a state write? (Tied to the call, repair 2.)"""
    p = _payload_arg(call)
    if p is None:
        return _expr_has_state(
            call
        )  # no payload= kw: fall back to the call's own args (still call-scoped)
    if _expr_has_state(p):  # inline dict/model literal with state
        return True
    r = _root_name(p)
    return bool(r and r in tainted)


def _state_writing_setpayload_sites(tree: ast.AST) -> list[tuple[str, int]]:
    """Every `set_payload(...)` whose payload argument carries a state write (via per-call dataflow),
    EXCLUDING creation functions (repair 3)."""
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def enclosing_func(n: ast.AST) -> ast.AST | None:
        cur = parent.get(n)
        while cur is not None and not isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
            cur = parent.get(cur)
        return cur

    sites: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set_payload"
        ):
            f = enclosing_func(node)
            if f is None or getattr(f, "name", "") in _CREATE_FUNCS:
                continue
            if _call_writes_state(node, _tainted_state_names(f)):
                sites.append((getattr(f, "name", "?"), node.lineno))
    return sites


def _scan_src_state_transition_violators() -> dict[str, list[tuple[str, int]]]:
    violators: dict[str, list[tuple[str, int]]] = {}
    for p in sorted(_SRC.rglob("*.py")):
        if str(p.relative_to(_SRC)) == _COORDINATOR_REL:  # repair 1: exact path, not basename
            continue
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        sites = _state_writing_setpayload_sites(tree)
        if sites:
            violators[str(p.relative_to(_SRC))] = sites
    return violators


#: Repair 4 — the PRESENT denominator: the exact (file, function) transition bypasses that exist today.
#: The strict-xfail G1 only proves >=1 violation; this pinned set proves the scanner still sees ALL of
#: them, so a bypass that silently disappears (scanner regression, or an unaccounted migration) FAILS
#: here instead of quietly shrinking the red. H5 updates this set as it migrates each path.
_PRESENT_TRANSITION_BYPASSES: set[tuple[str, str]] = {
    ("lifecycle/transitions.py", "transition"),
    ("planes/episodic/plane.py", "transition"),
    ("planes/concept/plane.py", "transition"),
    ("planes/thoughts/plane.py", "transition"),
    ("planes/artifact/plane.py", "transition"),
    ("planes/curated/plane.py", "transition"),
}


# G1 - CLOSURE-GATE (not Phase-1 acceptance): no direct state mutation outside the coordinator --------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="lifecycle state is mutated by set_payload in the 5 plane transition() methods + "
    "transitions.py - every one bypasses the (unbuilt) LifecycleTransitionCoordinator, so a bypassing "
    "path still produces mutation-without-audit. Flips green ONLY under slice-h5-unify-state-mutation.",
)
def test_g1_no_direct_state_transition_setpayload_outside_coordinator() -> None:
    """Closure-gate (NOT Phase-1 acceptance): C6b atomicity is not closed until EVERY state-writing
    transition `set_payload` routes through the coordinator. RED today; green only when H5 migrates them."""
    violators = _scan_src_state_transition_violators()
    if violators:
        flat = sorted(f"{f}:{ln}({fn})" for f, sites in violators.items() for fn, ln in sites)
        raise DefectStillPresent(
            f"{len(flat)} direct state-transition site(s) bypass LifecycleTransitionCoordinator "
            f"(migrate via slice-h5-unify-state-mutation): {flat}"
        )


def test_g1_present_denominator_control_sees_all_known_bypasses() -> None:
    """Repair 4 (green control): the scanner must see EXACTLY the known present bypasses. The strict-xfail
    above only proves >=1 exists - it could silently miss five and still look red. This fails if a known
    bypass disappears from the scanner without `_PRESENT_TRANSITION_BYPASSES` being updated (a scanner
    regression, or an unaccounted migration), and if a NEW bypass appears (exact equality)."""
    found = {
        (f, fn) for f, sites in _scan_src_state_transition_violators().items() for fn, _ in sites
    }
    assert found == _PRESENT_TRANSITION_BYPASSES, (
        f"scanner drift: missing={_PRESENT_TRANSITION_BYPASSES - found} "
        f"unexpected={found - _PRESENT_TRANSITION_BYPASSES} - account for every change (H5 migrations "
        "update this set as they land)"
    )


def test_g1_rule_discriminates_state_dataflow_from_unrelated_payloads() -> None:
    """Fixture/mechanism proof (green): the taint rule ties state to the SPECIFIC payload (repair 2).
    It flags a direct state model AND a chained state payload (the plane shape: data.update(state=...) ->
    model_validate(data) -> set_payload(payload=updated.model_dump())); it does NOT flag coordinator
    delegation (post-H5 shape), a non-state payload, OR a function that computes a state value but
    set_payloads an UNRELATED enrichment dict (the false-association Yua flagged)."""
    direct = ast.parse(
        "def transition(self, to_state):\n"
        "    updated = Row(state=to_state, version=self.v + 1)\n"
        "    self._client.set_payload(collection_name='c', payload=updated.model_dump())\n"
    )
    chained = ast.parse(  # the real plane dataflow: state flows through two hops
        "def transition(self, to_state):\n"
        "    data = self.current.model_dump()\n"
        "    data.update(state=to_state, version=1)\n"
        "    updated = Model.model_validate(data)\n"
        "    self._client.set_payload(collection_name='c', payload=updated.model_dump(mode='json'))\n"
    )
    delegated = ast.parse(
        "def transition(self, to_state):\n"
        "    return self._coordinator.transition(self._intent(to_state))\n"
    )
    enrichment = (
        ast.parse(  # a NON-state set_payload must NOT be flagged (maturation enrichment shape)
            "def _apply_enrichment(self, tags):\n"
            "    self._client.set_payload(collection_name='c', payload={'tags': tags})\n"
        )
    )
    false_assoc = (
        ast.parse(  # computes a state value AND separately writes an unrelated enrichment dict
            "def transition(self, to_state):\n"
            "    data = {}\n"
            "    data.update(state=to_state)\n"
            "    self._client.set_payload(collection_name='c', payload={'tags': self.tags})\n"
        )
    )
    assert _state_writing_setpayload_sites(direct), "must flag a direct state payload"
    assert _state_writing_setpayload_sites(chained), "must follow the taint chain to the payload"
    assert not _state_writing_setpayload_sites(delegated), "must clear coordinator delegation"
    assert not _state_writing_setpayload_sites(enrichment), "must not flag a non-state payload"
    assert not _state_writing_setpayload_sites(false_assoc), (
        "must NOT flag an unrelated enrichment set_payload just because the function computes state"
    )


# ============================================================================
# TRANCHE 2 - Phase-1 behavior harness + reds (drive the future coordinator)
# ============================================================================

_NS = "eric/claude-code/episodic"


@dataclass
class _Seed:
    collection: str
    object_id: str
    namespace: str
    version: int


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    """In-memory Qdrant with the canonical collection layout bootstrapped (one per test)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def seeded(qdrant: QdrantClient) -> _Seed:
    """A provisional episodic object to transition (real create via the plane + FakeEmbedder)."""
    plane = EpisodicPlane(client=qdrant, embedder=FakeEmbedder())
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=_NS, content="c6b-seed")))
    return _Seed(
        collection=str(collection_for_plane("episodic")),
        object_id=str(obj.object_id),
        namespace=_NS,
        version=int(obj.version),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "lifecycle.db"


class _TransientQdrantError(RuntimeError):
    """A transient/unknown Qdrant failure - the coordinator must keep the intent PENDING (retryable),
    never ABANDON it and never emit a FINAL audit (Yua corrections B/J)."""

    terminal = False


class _TerminalQdrantError(RuntimeError):
    """A proven-terminal Qdrant failure - the coordinator must ABANDON (never a FINAL event) (Yua J)."""

    terminal = True


def _fail_set_payload(client: QdrantClient, exc: Exception) -> None:
    if not hasattr(client, "_orig_set_payload"):
        client._orig_set_payload = client.set_payload  # type: ignore[attr-defined]

    def _boom(*_a: object, **_k: object) -> None:
        raise exc

    client.set_payload = _boom  # type: ignore[assignment]


def _restore_set_payload(client: QdrantClient) -> None:
    orig = getattr(client, "_orig_set_payload", None)
    if orig is not None:
        client.set_payload = orig  # type: ignore[method-assign]


def _qdrant_state(client: QdrantClient, collection: str, object_id: str) -> tuple[object, object]:
    """(version, state) of the object in Qdrant, or (None, None) if absent."""
    points, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=1,
        with_payload=True,
    )
    if not points:
        return (None, None)
    payload = points[0].payload or {}
    return (payload.get("version"), payload.get("state"))


def _api() -> Any:
    """Import-guard for the whole locked Phase-1 API (coordinator + TransitionIntent + outcome types).
    Absent today -> DefectStillPresent, so each red xfails for its OWN named reason (the specific behavior
    it asserts is unreachable), never a single shared 'coordinator absent'."""
    try:
        from musubi.lifecycle import coordinator as _c  # type: ignore[attr-defined]
    except ImportError as e:
        raise DefectStillPresent(
            "the Phase-1 coordinator module is not implemented (LifecycleTransitionCoordinator + "
            "TransitionIntent + TransitionFinal/TransitionPending)"
        ) from e
    return _c


def _coordinator(client: QdrantClient, db_path: Path) -> Any:
    return _api().LifecycleTransitionCoordinator(client=client, db_path=Path(db_path))


def _intent(
    seeded: _Seed,
    *,
    to_state: str,
    operation_key: str | None = None,
    expected_version: int | None = None,
) -> Any:
    """Build a frozen TransitionIntent (Yua-locked value object, not loose kwargs)."""
    return _api().TransitionIntent(
        collection=seeded.collection,
        object_id=seeded.object_id,
        namespace=seeded.namespace,
        expected_version=seeded.version if expected_version is None else expected_version,
        target_state=to_state,
        actor="t",
        reason="r",
        operation_key=operation_key,
    )


def _final_event_exists(db_path: Path, event_id: object) -> bool:
    """Whether a FINAL lifecycle_events row exists for this event_id (shared DB). False if the table is
    absent (C6 source not merged) or no row."""
    if event_id is None or not Path(db_path).exists():
        return False
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute("SELECT 1 FROM lifecycle_events WHERE event_id = ?", (event_id,))
        except sqlite3.OperationalError:
            return False
        return cur.fetchone() is not None
    finally:
        con.close()


def _outbox_rows(db_path: Path) -> list[dict[str, object]]:
    """Read the durable outbox directly (empty if the table does not exist yet)."""
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute(
                "SELECT operation_key, object_id, state, event_id FROM lifecycle_outbox"
            )
        except sqlite3.OperationalError:
            return []  # table absent
        return [
            {"operation_key": r[0], "object_id": r[1], "state": r[2], "event_id": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        con.close()


# R1 - durable PENDING intent committed BEFORE the Qdrant mutation ------------------------------------ #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="today transition() mutates Qdrant FIRST and records the audit only after, writing no PENDING "
    "outbox intent - so a mutation that faults (or the process dies) leaves NO durable record that the "
    "transition was even attempted. R1 requires a committed PENDING intent to exist before the mutation.",
)
def test_r1_durable_intent_persisted_before_qdrant_mutation(
    qdrant: QdrantClient,
    seeded: _Seed,
    db_path: Path,
) -> None:
    coord = _coordinator(qdrant, db_path)  # DefectStillPresent (xfail) until the coordinator exists
    _fail_set_payload(qdrant, _TransientQdrantError("injected transient during apply"))
    # Drive a transition whose Qdrant mutation will fault. A durable intent MUST already be committed
    # (written before the mutation) and, because the mutation did not succeed, it must be EXACTLY PENDING
    # with NO FINAL audit — APPLIED/FINAL here would be a false pre-mutation completion (Yua 2026-07-13).
    coord.transition(_intent(seeded, to_state="matured", operation_key="op-r1"))
    rows = [r for r in _outbox_rows(db_path) if r["operation_key"] == "op-r1"]
    if not rows:
        raise DefectStillPresent(
            "no durable intent was persisted before the Qdrant mutation "
            "(a mutate-first coordinator leaves no record when the mutation faults)"
        )
    if rows[0]["state"] != "PENDING":
        raise DefectStillPresent(
            f"a transient-faulted mutation must leave the intent EXACTLY PENDING, got "
            f"{rows[0]['state']!r} (APPLIED/FINAL would be a false pre-mutation completion)"
        )
    if _final_event_exists(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("no FINAL lifecycle event may exist for a still-PENDING operation")


# R2 - SQLite unavailable at begin => Qdrant UNTOUCHED + Err ------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="today transition() mutates Qdrant BEFORE any durable write, so a SQLite failure cannot "
    "prevent the mutation - the ordering guarantee (durable intent first) does not exist yet.",
)
def test_r2_sqlite_unavailable_blocks_qdrant_mutation(
    qdrant: QdrantClient,
    seeded: _Seed,
    db_path: Path,
) -> None:
    coord = _coordinator(qdrant, db_path)  # xfail until the coordinator exists
    before = _qdrant_state(qdrant, seeded.collection, seeded.object_id)
    os.chmod(
        db_path, 0
    )  # make the shared events+outbox DB unwritable => the PENDING write must fail
    try:
        try:
            res = coord.transition(_intent(seeded, to_state="matured", operation_key="op-r2"))
        except Exception as exc:
            raise DefectStillPresent(
                f"begin must return Err when the durable intent cannot be persisted, it raised: {exc!r}"
            ) from exc
    finally:
        os.chmod(db_path, 0o644)
    after = _qdrant_state(qdrant, seeded.collection, seeded.object_id)
    if before != after:
        raise DefectStillPresent(
            f"Qdrant was mutated despite the durable intent write being unavailable: {before} -> {after}"
        )
    if not isinstance(res, Err):
        raise DefectStillPresent(
            "SQLite unavailable at begin must yield Err (no future mutation), not a silent Ok/Pending"
        )


# R3 - transient Qdrant failure => Ok(Pending), no FINAL, reconciler later completes ------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="today a Qdrant failure returns a terminal Err with no durable intent and no reconciler, so a "
    "TRANSIENT failure cannot become a recoverable Ok(Pending) that a later reconcile completes.",
)
def test_r3_transient_failure_is_ok_pending_then_reconciles(
    qdrant: QdrantClient,
    seeded: _Seed,
    db_path: Path,
) -> None:
    coord = _coordinator(qdrant, db_path)  # xfail until the coordinator exists
    _fail_set_payload(qdrant, _TransientQdrantError("injected transient during apply"))
    res = coord.transition(_intent(seeded, to_state="matured", operation_key="op-r3"))
    if not isinstance(res, Ok):
        raise DefectStillPresent("a transient Qdrant failure must return Ok(Pending), not Err")
    outcome = res.value
    if type(outcome).__name__ != "TransitionPending":
        raise DefectStillPresent(
            f"a transient failure outcome must be TransitionPending, got {type(outcome).__name__}"
        )
    if getattr(outcome, "operation_key", None) != "op-r3" or not getattr(outcome, "event_id", None):
        raise DefectStillPresent("TransitionPending must carry operation_key + event_id")
    rows = [r for r in _outbox_rows(db_path) if r["operation_key"] == "op-r3"]
    if not rows or rows[0]["state"] != "PENDING":
        raise DefectStillPresent("the intent must remain PENDING after a transient failure")
    if _final_event_exists(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("no FINAL event may exist while the op is transient-PENDING")
    # Qdrant recovers; a reconciliation pass must complete the durable intent to FINAL.
    _restore_set_payload(qdrant)
    coord.reconcile_once(limit=10)
    rows2 = [r for r in _outbox_rows(db_path) if r["operation_key"] == "op-r3"]
    if not rows2 or rows2[0]["state"] != "FINAL":
        raise DefectStillPresent(
            "reconcile_once must finalize a recovered PENDING op (it stayed "
            f"{rows2[0]['state'] if rows2 else 'absent'!r})"
        )


# R4 - proven-terminal failure => Err + ABANDONED, never a FINAL event -------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="today a Qdrant failure returns a bare terminal Err with no durable ABANDONED record and no "
    "terminal-vs-transient classification, so a proven-terminal failure cannot be distinguished/recorded.",
)
def test_r4_terminal_failure_is_err_abandoned_no_final(
    qdrant: QdrantClient,
    seeded: _Seed,
    db_path: Path,
) -> None:
    coord = _coordinator(qdrant, db_path)  # xfail until the coordinator exists
    _fail_set_payload(qdrant, _TerminalQdrantError("injected terminal (proven)"))
    res = coord.transition(_intent(seeded, to_state="matured", operation_key="op-r4"))
    if not isinstance(res, Err):
        raise DefectStillPresent("a proven-terminal failure must return Err (no future mutation)")
    rows = [r for r in _outbox_rows(db_path) if r["operation_key"] == "op-r4"]
    if not rows or rows[0]["state"] != "ABANDONED":
        raise DefectStillPresent(
            f"a proven-terminal failure must mark the intent ABANDONED, got "
            f"{rows[0]['state'] if rows else 'no row'!r}"
        )
    if _final_event_exists(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("ABANDONED must never create a FINAL lifecycle event")
