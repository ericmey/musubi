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
Tranche 2: the Phase-1 behavior reds **R1-R8** driving the future `LifecycleTransitionCoordinator`
(R1-R4 durability/classification + R8 finalize atomicity over an in-memory Qdrant; R5-R7 crash matrix
C1/C2/C3 over a REAL subprocess + on-disk Qdrant). Each red is strict-xfail today because the coordinator
is unbuilt, but its decorator names the SPECIFIC defect and its assertion discriminates that behavior.
The evidence is RERUNNABLE: `test_red_proof_*` / `test_crash_red_proof_*` run a committed reference
coordinator + plausible-wrong candidates so the reviewer sees correct pass + every wrong fail (src stays
absent). R9-R22 + G2/G3 continue.

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
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.types.common import Err, Ok, generate_ksuid
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
# TRANCHE 2 - Phase-1 behavior harness + reds (drive the LOCKED coordinator API)
#
# Evidence model (Yua 2026-07-13): the red-proof must be RERUNNABLE, not commit prose. So this file
# COMMITS a reference coordinator (`_RefCoordinator`) + plausible-wrong candidates, and a green
# `test_red_proof_*` harness the reviewer runs: the correct candidate must satisfy every check, and each
# plausible-wrong candidate must fail its target check (the harness fails if any wrong candidate passes).
# The candidates are test-local and reached only via an `_ACTIVE_CANDIDATE` override, so `src/musubi`
# stays absent (the xfail reds still import the real, unbuilt coordinator and fail for their own reasons).
# ============================================================================

_NS = "eric/claude-code/episodic"


@dataclass
class _Seed:
    collection: str
    object_id: str
    namespace: str
    version: int


# ---- LOCKED value types (mirrored here as the reference/candidate surface) ------------------------- #


@dataclass(frozen=True)
class _RefIntent:
    collection: str
    object_id: str
    namespace: str
    expected_version: int
    target_state: str
    actor: str
    reason: str
    operation_key: str | None = None


@dataclass(frozen=True)
class _RefFinal:
    operation_key: str
    event_id: str
    kind: str = "final"


@dataclass(frozen=True)
class _RefPending:
    operation_key: str
    event_id: str
    kind: str = "pending"


@dataclass(frozen=True)
class _RefError:
    code: str


@dataclass(frozen=True)
class _RefReport:
    claimed: int = 0
    finalized: int = 0
    pending: int = 0
    abandoned: int = 0
    failed: int = 0


class _TransientQdrantError(RuntimeError):
    """A transient/unknown Qdrant failure - keep the intent PENDING (retryable) (Yua B/J)."""

    terminal = False


class _TerminalQdrantError(RuntimeError):
    """A proven-terminal Qdrant failure - ABANDON, never a FINAL event (Yua J)."""

    terminal = True


def _patch_sha(target_state: str, next_version: int) -> str:
    return hashlib.sha256(f"{target_state}:{next_version}".encode()).hexdigest()


# ---- committed reference/candidate coordinator (test-local, NEVER written to src) ------------------ #


class _RefCoordinator:
    """A reference implementation of the locked Phase-1 API. `mode` selects the correct behavior or a
    named plausible-wrong one so the red-proof discriminates each red. A private `_checkpoint(name)` seam
    (default no-op; NOT a public switch, NO production os._exit) lets tests inject a deterministic fault
    or crash at a named boundary."""

    def __init__(self, *, client: Any, db_path: Path, mode: str = "correct") -> None:
        self._client = client
        self._db = str(db_path)
        self._mode = mode
        self._checkpoint: Any = lambda _name: None
        con = sqlite3.connect(self._db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_outbox (operation_key TEXT PRIMARY KEY, object_id TEXT,"
            " collection TEXT, target_state TEXT, expected_version INTEGER, patch_sha TEXT,"
            " intent_digest TEXT, state TEXT, event_id TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_events (event_id TEXT PRIMARY KEY, object_id TEXT,"
            " namespace TEXT, to_state TEXT)"
        )
        if self._mode != "no_unique_index":
            # ATOMIC single-active-intent (Yua R11): a DB-enforced partial unique index, NOT a
            # check-then-insert. Two concurrent begins for one object can't both create a nonterminal row.
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_active_intent ON lifecycle_outbox "
                "(collection, object_id) WHERE state IN ('PENDING','APPLIED')"
            )
        con.commit()
        con.close()

    # -- internals --------------------------------------------------------------------------------- #
    def _key(self, i: _RefIntent) -> str:
        return (
            i.operation_key
            or f"canon:{i.collection}:{i.object_id}:{i.expected_version}:{i.target_state}"
        )

    def _cur(self, collection: str, object_id: str) -> tuple[object, object]:
        return _qdrant_state(self._client, collection, object_id)

    def _intent_digest(self, i: _RefIntent) -> str:
        """Canonical identity of the REQUESTED operation (Yua R10) - what an operation_key must map to. It
        binds EVERY event/patch-affecting field, including actor + reason (who/why), via a collision-safe
        canonical JSON encoding (NOT delimiter concatenation, which collides e.g. actor='a|b',reason='c'
        vs actor='a',reason='b|c'). A retry with the same key but any different field is a conflict."""
        fields: dict[str, object] = {
            "collection": i.collection,
            "object_id": i.object_id,
            "namespace": i.namespace,
            "expected_version": i.expected_version,
            "target_state": i.target_state,
            "actor": i.actor,
            "reason": i.reason,
        }
        if self._mode == "digest_no_actor":  # WRONG: omits actor -> misattributes who (R10)
            fields.pop("actor")
        if self._mode == "digest_no_reason":  # WRONG: omits reason -> misattributes why (R10)
            fields.pop("reason")
        if (
            self._mode == "delimiter_digest"
        ):  # WRONG: delimiter concat -> collision-vulnerable (R10)
            joined = "|".join(
                str(v)
                for v in (
                    i.collection,
                    i.object_id,
                    i.namespace,
                    i.expected_version,
                    i.target_state,
                    i.actor,
                    i.reason,
                )
            )
            return hashlib.sha256(joined.encode()).hexdigest()
        canon = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()

    def _write_pending(
        self, i: _RefIntent, opk: str, event_id: str, state: str = "PENDING"
    ) -> None:
        self._checkpoint("before_pending_commit")
        con = sqlite3.connect(self._db)
        try:
            # NOT "OR IGNORE": a partial-unique-index violation (a second active intent for the object)
            # must RAISE (IntegrityError) so the loser is rejected atomically, not silently ignored.
            con.execute(
                "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
                "expected_version,patch_sha,intent_digest,state,event_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    opk,
                    i.object_id,
                    i.collection,
                    i.target_state,
                    i.expected_version,
                    _patch_sha(i.target_state, i.expected_version + 1),
                    self._intent_digest(i),
                    state,
                    event_id,
                ),
            )
            con.commit()
        finally:
            con.close()
        self._checkpoint("after_pending_commit")

    def _mark(self, opk: str, state: str) -> None:
        con = sqlite3.connect(self._db)
        con.execute("UPDATE lifecycle_outbox SET state=? WHERE operation_key=?", (state, opk))
        con.commit()
        con.close()

    def _row_for_key(self, opk: str) -> tuple[str, str, str] | None:
        con = sqlite3.connect(self._db)
        try:
            cur = con.execute(
                "SELECT state, event_id, intent_digest FROM lifecycle_outbox WHERE operation_key=?",
                (opk,),
            )
            row = cur.fetchone()
            return (row[0], row[1], row[2]) if row else None
        finally:
            con.close()

    def _active_intent_for_object(
        self, collection: str, object_id: str, exclude_opk: str
    ) -> str | None:
        con = sqlite3.connect(self._db)
        try:
            cur = con.execute(
                "SELECT operation_key FROM lifecycle_outbox WHERE collection=? AND object_id=? "
                "AND state IN ('PENDING','APPLIED') AND operation_key != ?",
                (collection, object_id, exclude_opk),
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def _apply_conditional(
        self, collection: str, object_id: str, target_state: str, expected_version: int
    ) -> bool:
        """Server-side conditional: the expected version is IN the set_payload filter (object_id AND
        version == expected), so a stale version matches zero points and does not apply. Then readback the
        actual payload to confirm the fence held and the exact patch landed (Yua repair 5). The
        warn_only_fence mode (WRONG) drops the version condition -> a stale writer clobbers (R12/R22)."""
        must: list[models.Condition] = [
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))
        ]
        if self._mode != "warn_only_fence":
            must.append(
                models.FieldCondition(
                    key="version", match=models.MatchValue(value=expected_version)
                )
            )
        self._client.set_payload(
            collection_name=collection,
            payload={"state": target_state, "version": expected_version + 1},
            points=models.Filter(must=must),
        )
        ver, st = self._cur(collection, object_id)
        if self._mode == "warn_only_fence":
            return st == target_state  # ignores the version fence
        return ver == expected_version + 1 and st == target_state

    def _finalize(
        self, opk: str, event_id: str, object_id: str, namespace: str, target_state: str
    ) -> None:
        """Atomic FINAL: insert the FINAL lifecycle event AND mark the outbox FINAL in ONE txn. The
        outbox update is GUARDED on the exact APPLIED state (`WHERE state='APPLIED'`, exactly one row) so
        a PENDING row can never jump straight to FINAL (Yua R8 forward guard)."""
        if self._mode == "finalize_not_atomic":
            # WRONG: the event insert is its OWN committed txn, so a fault before the FINAL mark leaves the
            # event orphaned (not rolled back) - caught by R8's "rollback leaves NO event".
            con = sqlite3.connect(self._db)
            con.execute(
                "INSERT OR IGNORE INTO lifecycle_events (event_id,object_id,namespace,to_state) "
                "VALUES (?,?,?,?)",
                (event_id, object_id, namespace, target_state),
            )
            con.commit()
            con.close()
            self._checkpoint("inside_finalize_after_event_insert")
            con = sqlite3.connect(self._db)
            con.execute(
                "UPDATE lifecycle_outbox SET state='FINAL' WHERE operation_key=? AND state='APPLIED'",
                (opk,),
            )
            con.commit()
            con.close()
            return
        con = sqlite3.connect(self._db)
        try:
            con.execute("BEGIN")
            con.execute(
                "INSERT OR IGNORE INTO lifecycle_events (event_id,object_id,namespace,to_state) "
                "VALUES (?,?,?,?)",
                (event_id, object_id, namespace, target_state),
            )
            self._checkpoint("inside_finalize_after_event_insert")  # R8 fault point
            cur = con.execute(
                "UPDATE lifecycle_outbox SET state='FINAL' WHERE operation_key=? AND state='APPLIED'",
                (opk,),
            )
            if cur.rowcount != 1:
                con.execute("ROLLBACK")
                raise RuntimeError(
                    f"finalize guard: expected exactly one APPLIED row for {opk}, updated {cur.rowcount}"
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _classify_terminal(self, exc: Exception) -> bool:
        if self._mode == "classify_all_terminal":
            return True
        if self._mode == "classify_all_transient":
            return False
        return bool(getattr(exc, "terminal", False))

    # -- public API -------------------------------------------------------------------------------- #
    def transition(self, intent: _RefIntent) -> Any:
        # ignore_operation_key (WRONG): mint a fresh key each call -> a caller retry becomes a second
        # operation instead of the same one (caught by R10).
        opk = generate_ksuid() if self._mode == "ignore_operation_key" else self._key(intent)
        digest = self._intent_digest(intent)
        # operation_key idempotency + CONFLICT (R10): an existing row for this key short-circuits. If the
        # stored intent digest differs, the same key was reused for a DIFFERENT intent -> conflict, no
        # apply/new row/event. Only identical key+intent replays.
        if self._mode != "ignore_operation_key":
            existing = self._row_for_key(opk)
            if existing is not None:
                state, ev, stored_digest = existing
                if stored_digest != digest and self._mode != "trust_key_only":
                    return Err(error=_RefError(code="operation_key_conflict"))
                if state == "FINAL":
                    return Ok(value=_RefFinal(operation_key=opk, event_id=ev))
                if state == "ABANDONED":
                    return Err(error=_RefError(code="terminal_apply_failure"))
                return Ok(value=_RefPending(operation_key=opk, event_id=ev))  # in-flight: replay
        # no_unique_index (WRONG): a NAIVE check-then-insert for single-active-intent instead of the atomic
        # partial-unique index -> two simultaneous begins can both pass the check (caught by R11's race).
        if self._mode == "no_unique_index":
            other = self._active_intent_for_object(intent.collection, intent.object_id, opk)
            if other is not None:
                return Err(error=_RefError(code="active_intent_exists"))
        event_id = generate_ksuid()
        if self._mode != "mutate_first":
            try:
                self._write_pending(
                    intent,
                    opk,
                    event_id,
                    state=("FINAL" if self._mode == "premature_final" else "PENDING"),
                )
            except sqlite3.IntegrityError:
                # the ATOMIC partial-unique index rejected a second active intent for this object (R11).
                return Err(error=_RefError(code="active_intent_exists"))
            except sqlite3.Error:
                # A REAL SQLite failure at durable-begin (the checkpoint delivers sqlite3.OperationalError,
                # the same class a genuine disk error raises) maps to a bounded Err; no row, no mutation.
                if self._mode == "no_begin_catch":
                    raise  # WRONG: lets the store error escape past the mutation boundary
                return Err(error=_RefError(code="durable_begin_failed"))
        try:
            applied = self._apply_conditional(
                intent.collection, intent.object_id, intent.target_state, intent.expected_version
            )
            if not applied:
                if self._mode != "mutate_first":
                    self._mark(opk, "ABANDONED")
                return Err(error=_RefError(code="version_fence_violation"))
            self._checkpoint("after_qdrant_readback_before_applied_commit")
            self._mark(opk, "APPLIED")
            self._checkpoint("after_applied_commit_before_finalize")
        except Exception as exc:
            if self._classify_terminal(exc):
                if self._mode != "mutate_first":
                    self._mark(opk, "ABANDONED")
                return Err(error=_RefError(code="terminal_apply_failure"))
            return Ok(value=_RefPending(operation_key=opk, event_id=event_id))
        if self._mode == "mutate_first":
            self._write_pending(intent, opk, event_id)
        try:
            self._finalize(opk, event_id, intent.object_id, intent.namespace, intent.target_state)
        except Exception:
            # finalize failed (e.g. R8's injected fault inside the txn) -> the mutation is durable and the
            # row stays APPLIED; the reconciler will complete FINAL. Return Pending, not Final.
            return Ok(value=_RefPending(operation_key=opk, event_id=event_id))
        return Ok(value=_RefFinal(operation_key=opk, event_id=event_id))

    def reconcile_once(self, *, limit: int = 100) -> _RefReport:
        # reconcile_greedy (WRONG): also claims already-FINAL rows and re-affirms the payload -> a second
        # reconcile makes extra set_payload calls (caught by R3's exactly-one/zero-apply instrumentation).
        claim_states = (
            "('PENDING','APPLIED','FINAL')"
            if self._mode == "reconcile_greedy"
            else "('PENDING','APPLIED')"
        )
        con = sqlite3.connect(self._db)
        rows = con.execute(
            "SELECT operation_key,object_id,collection,target_state,expected_version,event_id,state "
            f"FROM lifecycle_outbox WHERE state IN {claim_states} LIMIT ?",
            (limit,),
        ).fetchall()
        con.close()
        fin = ab = pend = 0
        for opk, oid, coll, tstate, ver, event_id, state in rows:
            if state == "FINAL":  # only reachable under reconcile_greedy
                self._apply_conditional(
                    coll, oid, tstate, ver
                )  # WRONG: re-affirm -> extra apply call
                continue
            if state == "APPLIED":
                if self._mode == "reconcile_always_apply":
                    self._apply_conditional(
                        coll, oid, tstate, ver
                    )  # WRONG: re-apply an APPLIED row (C3)
                self._finalize(opk, event_id, oid, _NS, tstate)
                fin += 1
                continue
            if self._mode == "reconcile_no_apply":
                self._mark(opk, "APPLIED")
                self._finalize(opk, event_id, oid, _NS, tstate)  # WRONG: finalize without applying
                fin += 1
                continue
            # Readback FIRST (C2 recovery): if the mutation is already durably applied (a crash after the
            # Qdrant apply but before the APPLIED commit), recognize it and finalize WITHOUT re-applying.
            # reconcile_no_readback (WRONG) skips this and re-applies (an extra set_payload call, caught by
            # R6's zero-apply instrumentation).
            cur_ver, cur_st = self._cur(coll, oid)
            if self._mode != "reconcile_no_readback" and (cur_ver, cur_st) == (ver + 1, tstate):
                self._mark(opk, "APPLIED")
                self._finalize(opk, event_id, oid, _NS, tstate)
                fin += 1
                continue
            try:
                applied = self._apply_conditional(coll, oid, tstate, ver)
            except Exception as exc:
                if self._classify_terminal(exc):
                    self._mark(opk, "ABANDONED")
                    ab += 1
                else:
                    pend += 1
                continue
            if not applied:
                self._mark(opk, "ABANDONED")
                ab += 1
                continue
            self._mark(opk, "APPLIED")  # PENDING -> APPLIED before the guarded FINAL txn
            self._finalize(opk, event_id, oid, _NS, tstate)
            fin += 1
        return _RefReport(claimed=len(rows), finalized=fin, pending=pend, abandoned=ab)


class _CandidateApi:
    """Mimics the coordinator module namespace for a red-proof candidate."""

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self.TransitionIntent = _RefIntent
        self.TransitionFinal = _RefFinal
        self.TransitionPending = _RefPending

    def LifecycleTransitionCoordinator(self, *, client: Any, db_path: Path) -> _RefCoordinator:
        return _RefCoordinator(client=client, db_path=db_path, mode=self._mode)


_ACTIVE_CANDIDATE: _CandidateApi | None = None


@contextmanager
def _candidate(mode: str) -> Iterator[None]:
    global _ACTIVE_CANDIDATE
    _ACTIVE_CANDIDATE = _CandidateApi(mode)
    try:
        yield
    finally:
        _ACTIVE_CANDIDATE = None


def _api() -> Any:
    """Resolve the Phase-1 API: an active red-proof candidate if set (rerunnable evidence), else the real
    src coordinator. Absent today -> DefectStillPresent, so each xfail red fails for its OWN named reason."""
    if _ACTIVE_CANDIDATE is not None:
        return _ACTIVE_CANDIDATE
    try:
        from musubi.lifecycle import coordinator as _c  # type: ignore[attr-defined]
    except ImportError as e:
        raise DefectStillPresent(
            "the Phase-1 coordinator module is not implemented (LifecycleTransitionCoordinator + "
            "TransitionIntent + TransitionFinal/TransitionPending)"
        ) from e
    return _c


# ---- fixtures + shared helpers --------------------------------------------------------------------- #


def _make_env(base: Path) -> tuple[QdrantClient, _Seed, Path]:
    base.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    plane = EpisodicPlane(client=client, embedder=FakeEmbedder())
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=_NS, content="c6b-seed")))
    seed = _Seed(
        collection=str(collection_for_plane("episodic")),
        object_id=str(obj.object_id),
        namespace=_NS,
        version=int(obj.version),
    )
    return client, seed, base / "lifecycle.db"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[QdrantClient, _Seed, Path]]:
    client, seed, db_path = _make_env(tmp_path)
    try:
        yield (client, seed, db_path)
    finally:
        client.close()


def _coordinator(client: QdrantClient, db_path: Path) -> Any:
    return _api().LifecycleTransitionCoordinator(client=client, db_path=Path(db_path))


def _intent(
    seed: _Seed,
    *,
    to_state: str,
    operation_key: str | None = None,
    expected_version: int | None = None,
    actor: str = "t",
    reason: str = "r",
) -> Any:
    return _api().TransitionIntent(
        collection=seed.collection,
        object_id=seed.object_id,
        namespace=seed.namespace,
        expected_version=seed.version if expected_version is None else expected_version,
        target_state=to_state,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
    )


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


def _count_set_payload(client: QdrantClient) -> dict[str, int]:
    """Wrap the CURRENT set_payload to count effective apply calls (R3: first reconcile makes exactly one,
    a second makes zero - a duplicate idempotent write leaves the version unchanged but IS a call)."""
    orig: Any = client.set_payload
    calls = {"n": 0}

    def _wrapped(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return orig(*a, **k)

    client.set_payload = _wrapped  # type: ignore[method-assign]
    return calls


def _qdrant_state(client: QdrantClient, collection: str, object_id: str) -> tuple[object, object]:
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


def _outbox_rows(db_path: Path, operation_key: str) -> list[dict[str, object]]:
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute(
                "SELECT operation_key,state,event_id,patch_sha FROM lifecycle_outbox "
                "WHERE operation_key=?",
                (operation_key,),
            )
        except sqlite3.OperationalError:
            return []
        return [
            {"operation_key": r[0], "state": r[1], "event_id": r[2], "patch_sha": r[3]}
            for r in cur.fetchall()
        ]
    finally:
        con.close()


def _outbox_for_object(db_path: Path, object_id: str) -> list[dict[str, object]]:
    """Every outbox row for an object (to count operations/active-intents regardless of operation_key)."""
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute(
                "SELECT operation_key,state,event_id FROM lifecycle_outbox WHERE object_id=?",
                (object_id,),
            )
        except sqlite3.OperationalError:
            return []
        return [{"operation_key": r[0], "state": r[1], "event_id": r[2]} for r in cur.fetchall()]
    finally:
        con.close()


def _final_event_count(db_path: Path, event_id: object) -> int:
    if event_id is None or not Path(db_path).exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute("SELECT COUNT(*) FROM lifecycle_events WHERE event_id=?", (event_id,))
        except sqlite3.OperationalError:
            return 0
        return int(cur.fetchone()[0])
    finally:
        con.close()


def _set_checkpoint(coord: Any, hook: Any) -> None:
    coord._checkpoint = hook


# ---- check helpers (the actual behavioral assertions; run by both the xfail reds and the harness) -- #


def _check_r1(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    coord = _coordinator(client, db_path)
    _fail_set_payload(client, _TransientQdrantError("injected transient during apply"))
    coord.transition(_intent(seed, to_state="matured", operation_key="op-r1"))
    rows = _outbox_rows(db_path, "op-r1")
    if not rows:
        raise DefectStillPresent("no durable intent was persisted before the Qdrant mutation")
    if rows[0]["state"] != "PENDING":
        raise DefectStillPresent(
            f"a transient-faulted mutation must leave the intent EXACTLY PENDING, got {rows[0]['state']!r}"
        )
    if _final_event_count(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("no FINAL lifecycle event may exist for a still-PENDING operation")


def _check_r2(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    coord = _coordinator(client, db_path)
    before = _qdrant_state(client, seed.collection, seed.object_id)

    def _fail_begin(name: str) -> None:
        if name == "before_pending_commit":
            # A REAL sqlite failure class (Yua repair 1) - not a test-only exception a candidate could
            # special-case while genuine SQLite disk errors still escape.
            raise sqlite3.OperationalError("disk I/O error")

    _set_checkpoint(coord, _fail_begin)
    try:
        res = coord.transition(_intent(seed, to_state="matured", operation_key="op-r2"))
    except Exception as exc:
        raise DefectStillPresent(
            f"begin must return Err on durable-begin failure, it raised: {exc!r}"
        ) from exc
    if not isinstance(res, Err):
        raise DefectStillPresent("a durable-begin failure must return Err (no future mutation)")
    if getattr(res.error, "code", None) != "durable_begin_failed":
        raise DefectStillPresent(
            f"Err must identify the durable-begin failure, got code {getattr(res.error, 'code', None)!r}"
        )
    if _outbox_rows(db_path, "op-r2"):
        raise DefectStillPresent("a failed durable begin must persist NO outbox row")
    after = _qdrant_state(client, seed.collection, seed.object_id)
    if before != after:
        raise DefectStillPresent(
            f"Qdrant must be exactly unchanged when begin fails: {before} -> {after}"
        )


def _check_r3(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    coord = _coordinator(client, db_path)
    _fail_set_payload(client, _TransientQdrantError("injected transient during apply"))
    res = coord.transition(_intent(seed, to_state="matured", operation_key="op-r3"))
    if not isinstance(res, Ok):
        raise DefectStillPresent("a transient Qdrant failure must return Ok(Pending), not Err")
    if not isinstance(res.value, _api().TransitionPending):
        raise DefectStillPresent(
            f"transient outcome must be TransitionPending, got {type(res.value).__name__}"
        )
    rows = _outbox_rows(db_path, "op-r3")
    if len(rows) != 1 or rows[0]["state"] != "PENDING":
        raise DefectStillPresent("there must be EXACTLY ONE outbox row and it must be PENDING")
    if _final_event_count(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("no FINAL event may exist while the op is transient-PENDING")
    # Qdrant recovers; reconcile must ACTUALLY apply + finalize (not just flip a FINAL flag). Instrument
    # the real set_payload so a duplicate idempotent write (which leaves the version unchanged) is caught.
    _restore_set_payload(client)
    calls = _count_set_payload(client)
    before_first = calls["n"]
    report = coord.reconcile_once(limit=10)
    if calls["n"] - before_first != 1:
        raise DefectStillPresent(
            f"the first reconcile must make EXACTLY ONE effective apply, got {calls['n'] - before_first}"
        )
    ver, st = _qdrant_state(client, seed.collection, seed.object_id)
    if (ver, st) != (seed.version + 1, "matured"):
        raise DefectStillPresent(
            f"reconcile must actually apply the mutation (object -> matured/v{seed.version + 1}), "
            f"got {st}/{ver}"
        )
    rows2 = _outbox_rows(db_path, "op-r3")
    if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
        raise DefectStillPresent("there must be EXACTLY ONE outbox row and it must be FINAL")
    # patch-SHA readback: the stored intended patch SHA must equal the SHA of the ACTUAL Qdrant payload.
    if rows2[0]["patch_sha"] != _patch_sha(str(st), int(str(ver))):
        raise DefectStillPresent(
            "the stored intended patch SHA must match the readback of the actual Qdrant payload"
        )
    if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
        raise DefectStillPresent("recovery must produce EXACTLY ONE FINAL event for the event_id")
    if getattr(report, "finalized", None) != 1:
        raise DefectStillPresent(
            f"ReconcileReport must report exactly one finalized, got {report!r}"
        )
    # Second reconcile is a no-op: ZERO set_payload calls, no second event.
    before_second = calls["n"]
    report2 = coord.reconcile_once(limit=10)
    if calls["n"] - before_second != 0:
        raise DefectStillPresent(
            f"a second reconcile must make ZERO apply calls, got {calls['n'] - before_second}"
        )
    if getattr(report2, "finalized", 0) != 0:
        raise DefectStillPresent("a second reconcile must finalize nothing")
    if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
        raise DefectStillPresent("a second reconcile must not emit a second FINAL event")


def _check_r4(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    coord = _coordinator(client, db_path)
    before = _qdrant_state(client, seed.collection, seed.object_id)
    _fail_set_payload(client, _TerminalQdrantError("injected terminal (proven)"))
    res = coord.transition(_intent(seed, to_state="matured", operation_key="op-r4"))
    if not isinstance(res, Err):
        raise DefectStillPresent("a proven-terminal failure must return Err (no future mutation)")
    if getattr(res.error, "code", None) != "terminal_apply_failure":
        raise DefectStillPresent(
            f"Err must carry the exact bounded terminal code 'terminal_apply_failure', got "
            f"{getattr(res.error, 'code', None)!r}"
        )
    after = _qdrant_state(client, seed.collection, seed.object_id)
    if before != after:
        raise DefectStillPresent(
            f"a terminal failure must leave Qdrant exactly unchanged: {before} -> {after}"
        )
    rows = _outbox_rows(db_path, "op-r4")
    if len(rows) != 1 or rows[0]["state"] != "ABANDONED":
        raise DefectStillPresent(
            f"a proven-terminal failure must leave EXACTLY ONE ABANDONED row, got "
            f"{[r['state'] for r in rows] if rows else 'no row'}"
        )
    if _final_event_count(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("ABANDONED must never create a FINAL lifecycle event")
    # A later reconcile can never resurrect an ABANDONED op into a mutation or FINAL.
    _restore_set_payload(client)
    coord.reconcile_once(limit=10)
    after2 = _qdrant_state(client, seed.collection, seed.object_id)
    rows2 = _outbox_rows(db_path, "op-r4")
    if after2 != before:
        raise DefectStillPresent("reconcile must never mutate for an ABANDONED op")
    if not rows2 or rows2[0]["state"] != "ABANDONED":
        raise DefectStillPresent("an ABANDONED op must stay ABANDONED across reconcile")
    if _final_event_count(db_path, rows2[0]["event_id"]):
        raise DefectStillPresent("reconcile must never emit a FINAL event for an ABANDONED op")


# ---- xfail reds: run the checks against the REAL (unbuilt) coordinator -> DefectStillPresent -------- #


_R1_REASON = (
    "today transition() mutates Qdrant FIRST and records the audit only after, writing no PENDING outbox "
    "intent - a faulted mutation leaves no durable record. R1: a committed PENDING intent before mutation."
)
_R2_REASON = (
    "today transition() mutates Qdrant before any durable write, so a durable-begin failure cannot keep "
    "Qdrant untouched. R2: a deterministic begin-persist failpoint yields Err(durable_begin), no row, no "
    "mutation."
)
_R3_REASON = (
    "today a Qdrant failure is a bare terminal Err with no durable intent and no reconciler, so a TRANSIENT "
    "failure cannot become Ok(Pending) that a later reconcile actually applies + finalizes exactly once."
)
_R4_REASON = (
    "today a Qdrant failure returns a bare Err with no durable ABANDONED record and no terminal/transient "
    "classification, so a proven-terminal failure cannot be recorded ABANDONED + provably never mutate."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R1_REASON)
def test_r1_durable_intent_persisted_before_qdrant_mutation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r1(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R2_REASON)
def test_r2_durable_begin_failure_blocks_qdrant_mutation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r2(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R3_REASON)
def test_r3_transient_failure_is_ok_pending_then_reconciles(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r3(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R4_REASON)
def test_r4_terminal_failure_is_err_abandoned_no_final(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r4(*env)


# ---- R10-R12 + R22: operation_key idempotency, single active intent, hard fence, two-tx race ------- #


def _check_r10(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """operation_key idempotency + CONFLICT (Yua correction C, a digest not key-alone):
    (a) identical key + identical intent REPLAYS (one operation, one FINAL, one event, zero re-apply);
    (b) same key + DIFFERENT intent => exact operation_key_conflict, no apply / no new row / no event."""
    coord = _coordinator(client, db_path)
    intent = _intent(seed, to_state="matured", operation_key="op-r10")
    r1 = coord.transition(intent)
    if not isinstance(r1, Ok):
        raise DefectStillPresent("the first transition must succeed")
    # (a) identical retry replays with NO second apply.
    calls = _count_set_payload(client)
    n0 = calls["n"]
    r2 = coord.transition(intent)  # a caller RETRY with the SAME key AND the SAME intent
    if calls["n"] - n0 != 0:
        raise DefectStillPresent("an identical retry must NOT trigger a second Qdrant apply")
    if not isinstance(r2, Ok) or getattr(r2.value, "operation_key", None) != "op-r10":
        raise DefectStillPresent("an identical retry must replay the SAME completed operation (Ok)")
    rows = _outbox_for_object(db_path, seed.object_id)
    if len(rows) != 1 or rows[0]["state"] != "FINAL":
        raise DefectStillPresent(
            f"an identical retry must leave EXACTLY ONE FINAL row, got {[r['state'] for r in rows]}"
        )
    if _final_event_count(db_path, rows[0]["event_id"]) != 1:
        raise DefectStillPresent("an identical retry must produce EXACTLY ONE FINAL event")
    if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
        raise DefectStillPresent("the object must be at target after exactly one effective apply")

    # (b-d) same key op-r10 but a DIFFERENT event-affecting field => exact operation_key_conflict, with
    # NO apply / NO new row / NO event / NO object change. Yua: bind target AND actor AND reason.
    def _expect_conflict(bad: Any, why: str) -> None:
        before_calls = calls["n"]
        rc = coord.transition(bad)
        if not isinstance(rc, Err) or getattr(rc.error, "code", None) != "operation_key_conflict":
            raise DefectStillPresent(
                f"reusing an operation_key for a DIFFERENT {why} must be conflict"
            )
        if calls["n"] - before_calls != 0:
            raise DefectStillPresent(f"an operation_key {why}-conflict must NOT apply anything")
        if len(_outbox_for_object(db_path, seed.object_id)) != 1:
            raise DefectStillPresent(
                f"an operation_key {why}-conflict must NOT create a new outbox row"
            )
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent(f"an operation_key {why}-conflict must not change the object")

    _expect_conflict(_intent(seed, to_state="demoted", operation_key="op-r10"), "target")
    _expect_conflict(
        _intent(seed, to_state="matured", operation_key="op-r10", actor="other"), "actor"
    )
    _expect_conflict(
        _intent(seed, to_state="matured", operation_key="op-r10", reason="other"), "reason"
    )
    # (e) delimiter-collision adversarial: (actor='a|b', reason='c') and (actor='a', reason='b|c') are the
    # SAME under a naive '|' join but DIFFERENT operations - the canonical encoding must treat them as a
    # conflict, not a replay (a delimiter_digest candidate collides them and replays -> caught here).
    op = "op-r10-delim"
    ra = coord.transition(
        _intent(
            seed,
            to_state="matured",
            operation_key=op,
            expected_version=seed.version + 1,
            actor="a|b",
            reason="c",
        )
    )
    if not isinstance(ra, Ok):
        raise DefectStillPresent("the delimiter-adversarial first op must succeed")
    before_calls = calls["n"]
    rb = coord.transition(
        _intent(
            seed,
            to_state="matured",
            operation_key=op,
            expected_version=seed.version + 1,
            actor="a",
            reason="b|c",
        )
    )
    if not isinstance(rb, Err) or getattr(rb.error, "code", None) != "operation_key_conflict":
        raise DefectStillPresent(
            "two intents that collide under a naive '|' join must be an operation_key_conflict, "
            "not a replay (the digest must use a collision-safe canonical encoding)"
        )
    if calls["n"] - before_calls != 0:
        raise DefectStillPresent("the delimiter-collision conflict must NOT apply anything")


# ---- R11: single active intent proven under a TWO-PROCESS begin race (Yua correction D) ------------ #
#
# Yua rejected the sequential version: `_active_intent_for_object` then INSERT is check-then-insert, so
# two simultaneous begins can both pass. This proves the ATOMIC partial-unique index with two REAL OS
# processes synchronized at the `before_pending_commit` boundary (a file barrier), so both hit the INSERT
# together. Exactly one is accepted (exit _WIN); the other is rejected active_intent_exists (exit
# _CONFLICT); the shared SQLite holds exactly one nonterminal row. A naive check-then-insert candidate
# (no_unique_index) lets both through -> two _WIN exits (caught).

_RACE_WIN = 21
_RACE_CONFLICT = 22
_RACE_BARRIER_TIMEOUT = 30.0


def _barrier_source(barrier_dir: Path, tag: str, n: int) -> str:
    """Inline source (used inside a child's checkpoint) that rendezvouses n processes at a file barrier."""
    return (
        "import os as _os, time as _time\n"
        f"_bd = {str(barrier_dir)!r}\n"
        "def _await_barrier():\n"
        # write the ready-file AT the barrier (a true rendezvous): N files present => all N processes have
        # REACHED this point, so nobody proceeds past the barrier until every process is here.
        f"    open(_os.path.join(_bd, {tag!r} + '.' + str(_os.getpid())), 'w').close()\n"
        f"    for _ in range(int({_RACE_BARRIER_TIMEOUT!r} / 0.02)):\n"
        f"        if len([f for f in _os.listdir(_bd) if f.startswith({tag!r})]) >= {n}: return\n"
        "        _time.sleep(0.02)\n"
        "    raise SystemExit('barrier timeout')\n"
    )


def _race_child_source(
    *, mode: str, db_path: Path, seed: _Seed, target: str, op_key: str, barrier_dir: Path
) -> str:
    return (
        "import os, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord, _RefIntent as _Intent\n"
        + _barrier_source(barrier_dir, "begin", 2)
        + "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    _client = QdrantClient(':memory:')\n"
        f"_c = _Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})\n"
        "def _cp(name):\n"
        "    if name == 'before_pending_commit': _await_barrier()\n"
        f"    if name == 'after_pending_commit': os._exit({_RACE_WIN})\n"
        "_c._checkpoint = _cp\n"
        f"_res = _c.transition(_Intent(collection={seed.collection!r}, object_id={seed.object_id!r}, "
        f"namespace={seed.namespace!r}, expected_version={seed.version}, target_state={target!r}, "
        f"actor='t', reason='r', operation_key={op_key!r}))\n"
        "code = getattr(getattr(_res, 'error', None), 'code', None)\n"
        f"os._exit({_RACE_CONFLICT} if code == 'active_intent_exists' else 99)\n"
    )


def _run_two_process_race(
    base: Path, *, mode: str, targets: tuple[str, str], op_keys: tuple[str, str]
) -> tuple[Path, _Seed, list[int]]:
    """Pre-create the shared schema, then spawn two children that race the begin boundary. Returns
    (db_path, seed, [returncode_a, returncode_b])."""
    _api()  # xfail today (coordinator absent)
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "lifecycle.db"
    barrier_dir = base / "barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    seed = _Seed(
        collection=str(collection_for_plane("episodic")),
        object_id="raceobj0000000000000000000",
        namespace=_NS,
        version=1,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _RefCoordinator(
            client=QdrantClient(":memory:"), db_path=db_path, mode=mode
        )  # create schema+index
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _race_child_source(
                    mode=mode,
                    db_path=db_path,
                    seed=seed,
                    target=targets[i],
                    op_key=op_keys[i],
                    barrier_dir=barrier_dir,
                ),
            ]
        )
        for i in range(2)
    ]
    codes = [p.wait(timeout=90) for p in procs]
    return db_path, seed, codes


def _check_r11(base: Path) -> None:
    db_path, seed, codes = _run_two_process_race(
        base,
        mode=(_ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"),
        targets=("matured", "matured"),
        op_keys=("op-a11", "op-b11"),
    )
    if sorted(codes) != [_RACE_WIN, _RACE_CONFLICT]:
        raise DefectStillPresent(
            f"exactly one begin must be accepted and one rejected active_intent_exists; got exit codes "
            f"{codes} (both {_RACE_WIN} = both accepted = the check-then-insert race)"
        )
    nonterminal = [
        r
        for r in _outbox_for_object(db_path, seed.object_id)
        if r["state"] in ("PENDING", "APPLIED")
    ]
    if len(nonterminal) != 1:
        raise DefectStillPresent(
            f"the shared outbox must hold EXACTLY ONE nonterminal intent, got {len(nonterminal)}"
        )


def _check_r12(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Hard version fence: a stale expected_version is refused (Err), the mutation is NOT applied, and the
    intent is ABANDONED - never a warn-only last-writer-wins clobber (Yua fork 2 / correction E)."""
    coord = _coordinator(client, db_path)
    before = _qdrant_state(client, seed.collection, seed.object_id)
    stale = _intent(
        seed, to_state="matured", operation_key="op-r12", expected_version=seed.version - 1
    )
    res = coord.transition(stale)
    if not isinstance(res, Err):
        raise DefectStillPresent("a stale expected_version must be REFUSED (Err), not applied")
    after = _qdrant_state(client, seed.collection, seed.object_id)
    if after != before:
        raise DefectStillPresent(
            f"a fenced-out transition must NOT mutate Qdrant: {before} -> {after}"
        )
    rows = _outbox_rows(db_path, "op-r12")
    if not rows or rows[0]["state"] != "ABANDONED":
        raise DefectStillPresent("a fence violation must leave the intent ABANDONED")
    if _final_event_count(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("a fenced-out transition must never emit a FINAL event")


_R10_REASON = (
    "today an event_id is minted per call with no operation_key, so a caller RETRY of the same logical "
    "request creates a second operation; R10 needs one operation/FINAL/event/apply per operation_key."
)
_R11_REASON = (
    "today nothing enforces one active intent per object, so two in-flight transitions on one object can "
    "both mutate and a later one can hide a crash-applied earlier one; R11 needs the second rejected."
)
_R12_REASON = (
    "today expected_version is warn-only (last writer wins), so a stale writer clobbers; R12 needs a hard "
    "fence: stale expected => Err + ABANDONED, mutation not applied."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R10_REASON)
def test_r10_operation_key_idempotent_across_caller_retries(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r10(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R11_REASON)
def test_r11_single_active_intent_per_object(tmp_path: Path) -> None:
    _check_r11(tmp_path)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R12_REASON)
def test_r12_hard_version_fence_refuses_stale(env: tuple[QdrantClient, _Seed, Path]) -> None:
    _check_r12(*env)


# ---- R5-R8: crash matrix + finalize atomicity (Yua locked ruling) --------------------------------- #
#
# R5-R7 use a REAL subprocess that os._exit(DISTINCT_CODE) at a named checkpoint, over an ON-DISK local
# Qdrant shared sequentially across processes (parent seeds+closes -> child opens/applies/exits ->
# parent reopens; OS death releases the lock, bounded reopen retry). The mutation observed after the
# child dies is the child's ACTUAL side effect (Yua rejected parent-modeled Qdrant). Every child exit is
# the named nonzero failpoint code, never a timeout/kill. R8 is in-process: a fault injected INSIDE the
# real SQLite FINAL transaction after the event insert.

_C1_CODE = 41  # after_pending_commit
_C2_CODE = 42  # after_qdrant_readback_before_applied_commit
_C3_CODE = 43  # after_applied_commit_before_finalize


def _make_ondisk_env(base: Path) -> tuple[_Seed, Path, Path]:
    """An ON-DISK Qdrant (path mode) + shared SQLite, seeded, with the Qdrant client CLOSED so a child
    process can open it. Returns (seed, db_path, qdrant_path)."""
    base.mkdir(parents=True, exist_ok=True)
    qdrant_path = base / "qd"
    db_path = base / "lifecycle.db"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(path=str(qdrant_path))
    bootstrap(client)
    plane = EpisodicPlane(client=client, embedder=FakeEmbedder())
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=_NS, content="c6b-crash-seed")))
    seed = _Seed(
        collection=str(collection_for_plane("episodic")),
        object_id=str(obj.object_id),
        namespace=_NS,
        version=int(obj.version),
    )
    client.close()  # release the lock so the child can open the same path
    return seed, db_path, qdrant_path


def _open_ondisk_qdrant(qdrant_path: Path) -> QdrantClient:
    """Reopen the on-disk Qdrant with a BOUNDED retry (the child's os._exit releases the OS lock)."""
    last: Exception | None = None
    for _ in range(50):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return QdrantClient(path=str(qdrant_path))
        except Exception as e:
            last = e
            time.sleep(0.1)
    raise DefectStillPresent(f"could not reopen the on-disk Qdrant after the child exit: {last!r}")


def _crash_child_source(
    *,
    use_reference: bool,
    mode: str,
    qdrant_path: Path,
    db_path: Path,
    seed: _Seed,
    crash_at: str,
    exit_code: int,
    op_key: str,
) -> str:
    if use_reference:
        imp = (
            "from tests.lifecycle.test_c6b_atomicity import "
            "_RefCoordinator as _Coord, _RefIntent as _Intent"
        )
        ctor = f"_Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})"
    else:  # future: drive the REAL src coordinator (absent today -> the parent xfails at _api())
        imp = (
            "from musubi.lifecycle.coordinator import "
            "LifecycleTransitionCoordinator as _Coord, TransitionIntent as _Intent"
        )
        ctor = f"_Coord(client=_client, db_path={str(db_path)!r})"
    return (
        "import os, warnings\n"
        "from qdrant_client import QdrantClient\n"
        f"{imp}\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        f"    _client = QdrantClient(path={str(qdrant_path)!r})\n"
        f"_c = {ctor}\n"
        f"_c._checkpoint = lambda name: os._exit({exit_code}) if name == {crash_at!r} else None\n"
        f"_c.transition(_Intent(collection={seed.collection!r}, object_id={seed.object_id!r}, "
        f"namespace={seed.namespace!r}, expected_version={seed.version}, target_state='matured', "
        f"actor='t', reason='r', operation_key={op_key!r}))\n"
        "os._exit(99)\n"  # reached only if the checkpoint did NOT fire -> a distinct 'no crash' code
    )


def _drive_crash(
    base: Path, crash_at: str, exit_code: int, op_key: str
) -> tuple[_Seed, Path, Path]:
    _api()  # xfail today (coordinator absent); in red-proof a candidate is active
    use_ref = _ACTIVE_CANDIDATE is not None
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    seed, db_path, qdrant_path = _make_ondisk_env(base)
    src = _crash_child_source(
        use_reference=use_ref,
        mode=mode,
        qdrant_path=qdrant_path,
        db_path=db_path,
        seed=seed,
        crash_at=crash_at,
        exit_code=exit_code,
        op_key=op_key,
    )
    proc = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=90, check=False)
    if proc.returncode != exit_code:
        raise DefectStillPresent(
            f"child must exit at the {crash_at} checkpoint with code {exit_code} (never timeout/kill), "
            f"got {proc.returncode}: {proc.stderr.decode()[-300:]}"
        )
    return seed, db_path, qdrant_path


def _check_r5(base: Path) -> None:  # C1: crash after PENDING, before Qdrant
    seed, db_path, qdrant_path = _drive_crash(base, "after_pending_commit", _C1_CODE, "op-r5")
    rows = _outbox_rows(db_path, "op-r5")
    if len(rows) != 1 or rows[0]["state"] != "PENDING":
        raise DefectStillPresent(
            f"a crash before Qdrant must leave EXACTLY ONE PENDING row, got {[r['state'] for r in rows]}"
        )
    client = _open_ondisk_qdrant(qdrant_path)
    try:
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version, "provisional"):
            raise DefectStillPresent("Qdrant must be UNCHANGED after a crash before the mutation")
        coord = _coordinator(client, db_path)
        calls = _count_set_payload(client)
        n0 = calls["n"]
        report = coord.reconcile_once(limit=10)
        if calls["n"] - n0 != 1:
            raise DefectStillPresent(f"reconcile must apply EXACTLY ONCE, got {calls['n'] - n0}")
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent("reconcile must apply the mutation (object -> matured/v+1)")
        rows2 = _outbox_rows(db_path, "op-r5")
        if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
            raise DefectStillPresent(
                "reconcile must finalize the recovered op (exactly one FINAL row)"
            )
        if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
            raise DefectStillPresent("recovery must produce EXACTLY ONE FINAL event")
        if getattr(report, "finalized", None) != 1:
            raise DefectStillPresent(f"report must show one finalized, got {report!r}")
        n1 = calls["n"]
        coord.reconcile_once(limit=10)
        if calls["n"] - n1 != 0:
            raise DefectStillPresent("a second reconcile must make ZERO applies")
    finally:
        client.close()


def _check_r6(base: Path) -> None:  # C2: crash after Qdrant apply, before APPLIED commit
    seed, db_path, qdrant_path = _drive_crash(
        base, "after_qdrant_readback_before_applied_commit", _C2_CODE, "op-r6"
    )
    rows = _outbox_rows(db_path, "op-r6")
    if len(rows) != 1 or rows[0]["state"] != "PENDING":
        raise DefectStillPresent(
            f"a crash post-apply/pre-APPLIED must leave EXACTLY ONE PENDING row, "
            f"got {[r['state'] for r in rows]}"
        )
    client = _open_ondisk_qdrant(qdrant_path)
    try:
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent(
                "the child's Qdrant mutation must be durable (the ACTUAL cross-process side effect)"
            )
        coord = _coordinator(client, db_path)
        calls = _count_set_payload(client)
        n0 = calls["n"]
        report = coord.reconcile_once(limit=10)
        if calls["n"] - n0 != 0:
            raise DefectStillPresent(
                f"reconcile must RECOGNIZE the already-applied mutation and NOT re-apply, "
                f"got {calls['n'] - n0} applies"
            )
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent("reconcile must not change the already-correct Qdrant state")
        rows2 = _outbox_rows(db_path, "op-r6")
        if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
            raise DefectStillPresent(
                "reconcile must finalize the recovered op (exactly one FINAL row)"
            )
        if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
            raise DefectStillPresent("recovery must produce EXACTLY ONE FINAL event")
        if getattr(report, "finalized", None) != 1:
            raise DefectStillPresent(f"report must show one finalized, got {report!r}")
        n1 = calls["n"]
        coord.reconcile_once(limit=10)
        if calls["n"] - n1 != 0:
            raise DefectStillPresent("a second reconcile must make ZERO applies")
    finally:
        client.close()


def _check_r7(base: Path) -> None:  # C3: crash after APPLIED commit, before finalize
    seed, db_path, qdrant_path = _drive_crash(
        base, "after_applied_commit_before_finalize", _C3_CODE, "op-r7"
    )
    rows = _outbox_rows(db_path, "op-r7")
    if len(rows) != 1 or rows[0]["state"] != "APPLIED":
        raise DefectStillPresent(
            f"a crash post-APPLIED/pre-finalize must leave EXACTLY ONE APPLIED row, "
            f"got {[r['state'] for r in rows]}"
        )
    if _final_event_count(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("no FINAL event may exist before finalize")
    client = _open_ondisk_qdrant(qdrant_path)
    try:
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent("the child's Qdrant mutation must be durable")
        coord = _coordinator(client, db_path)
        calls = _count_set_payload(client)
        n0 = calls["n"]
        report = coord.reconcile_once(limit=10)
        if calls["n"] - n0 != 0:
            raise DefectStillPresent(
                f"reconcile of an APPLIED row must perform ONLY the atomic FINAL, no apply; "
                f"got {calls['n'] - n0} applies"
            )
        rows2 = _outbox_rows(db_path, "op-r7")
        if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
            raise DefectStillPresent(
                "reconcile must finalize the APPLIED row (exactly one FINAL row)"
            )
        if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
            raise DefectStillPresent("finalize must produce EXACTLY ONE event")
        if getattr(report, "finalized", None) != 1:
            raise DefectStillPresent(f"report must show one finalized, got {report!r}")
        n1 = calls["n"]
        coord.reconcile_once(limit=10)
        if calls["n"] - n1 != 0:
            raise DefectStillPresent("a second reconcile must be a no-op")
    finally:
        client.close()


def _check_r8(client: QdrantClient, seed: _Seed, db_path: Path) -> None:  # finalize-txn atomicity
    coord = _coordinator(client, db_path)

    def _fault(name: str) -> None:
        if name == "inside_finalize_after_event_insert":
            raise RuntimeError("injected finalize-txn fault after the event insert")

    _set_checkpoint(coord, _fault)
    coord.transition(_intent(seed, to_state="matured", operation_key="op-r8"))
    rows = _outbox_rows(db_path, "op-r8")
    if len(rows) != 1 or rows[0]["state"] != "APPLIED":
        raise DefectStillPresent(
            f"a rolled-back finalize must leave the outbox EXACTLY APPLIED (not PENDING/FINAL), "
            f"got {[r['state'] for r in rows]}"
        )
    if _final_event_count(db_path, rows[0]["event_id"]):
        raise DefectStillPresent("a rolled-back finalize transaction must leave NO event")
    if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
        raise DefectStillPresent("the Qdrant mutation must be applied + durable before finalize")
    # finalize retried by reconcile: exactly one FINAL/event, NO second apply.
    _set_checkpoint(coord, lambda _name: None)
    calls = _count_set_payload(client)
    n0 = calls["n"]
    report = coord.reconcile_once(limit=10)
    if calls["n"] - n0 != 0:
        raise DefectStillPresent("reconcile of an APPLIED row must NOT re-apply Qdrant")
    rows2 = _outbox_rows(db_path, "op-r8")
    if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
        raise DefectStillPresent("reconcile must finalize the APPLIED row to FINAL")
    if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
        raise DefectStillPresent("finalize must produce EXACTLY ONE event")
    if getattr(report, "finalized", None) != 1:
        raise DefectStillPresent(f"report must show one finalized, got {report!r}")
    n1 = calls["n"]
    coord.reconcile_once(limit=10)
    if calls["n"] - n1 != 0:
        raise DefectStillPresent("a second reconcile must be a no-op")


_R5_REASON = (
    "today there is no durable pre-Qdrant intent, so a process death before the mutation leaves no "
    "recoverable record; R5 needs a committed PENDING that survives the crash and a reconcile that "
    "applies + finalizes exactly once."
)
_R6_REASON = (
    "today there is no outbox/reconciler, so a crash after the Qdrant mutation but before recording it "
    "loses the audit; R6 needs reconcile to recognize the already-applied mutation and finalize WITHOUT "
    "re-applying."
)
_R7_REASON = (
    "today there is no APPLIED state, so a crash after the mutation but before the audit leaves "
    "mutation-without-audit; R7 needs an APPLIED row whose reconcile performs only the atomic FINAL."
)
_R8_REASON = (
    "today the audit write is not transactional with the outbox, so a fault mid-finalize can orphan an "
    "event or half-finalize; R8 needs the FINAL event insert + outbox->FINAL in ONE txn that rolls back "
    "cleanly to EXACTLY APPLIED."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R5_REASON)
def test_r5_crash_after_pending_before_qdrant(tmp_path: Path) -> None:
    _check_r5(tmp_path)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R6_REASON)
def test_r6_crash_after_qdrant_before_applied(tmp_path: Path) -> None:
    _check_r6(tmp_path)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R7_REASON)
def test_r7_crash_after_applied_before_finalize(tmp_path: Path) -> None:
    _check_r7(tmp_path)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R8_REASON)
def test_r8_finalize_transaction_is_atomic(env: tuple[QdrantClient, _Seed, Path]) -> None:
    _check_r8(*env)


# ---- committed, RERUNNABLE red-proof harness (Yua evidence rule) ----------------------------------- #

_RED_PROOF: dict[str, tuple[Any, list[str]]] = {
    # red -> (check, plausible-wrong modes that MUST fail this check)
    "r1": (_check_r1, ["mutate_first", "premature_final"]),
    "r2": (_check_r2, ["mutate_first", "no_begin_catch"]),
    "r3": (_check_r3, ["classify_all_terminal", "reconcile_no_apply", "reconcile_greedy"]),
    "r4": (_check_r4, ["classify_all_transient"]),
    "r8": (_check_r8, ["finalize_not_atomic"]),
    "r10": (
        _check_r10,
        [
            "ignore_operation_key",
            "trust_key_only",
            "digest_no_actor",
            "digest_no_reason",
            "delimiter_digest",
        ],
    ),
    "r12": (_check_r12, ["warn_only_fence"]),
}


@pytest.mark.parametrize("red", sorted(_RED_PROOF))
def test_red_proof_correct_passes_and_wrong_fails(red: str, tmp_path: Path) -> None:
    """RERUNNABLE evidence: the CORRECT candidate must satisfy the check; every plausible-wrong candidate
    must fail it (this test fails if any wrong candidate passes). Candidates are test-local; src stays
    absent."""
    check, wrongs = _RED_PROOF[red]

    with _candidate("correct"):
        client, seed, db_path = _make_env(tmp_path / f"{red}-correct")
        try:
            check(client, seed, db_path)  # must NOT raise
        except (
            DefectStillPresent
        ) as e:  # pragma: no cover - a correct candidate failing is a real defect
            raise AssertionError(f"correct candidate failed {red}: {e}") from e
        finally:
            client.close()

    for mode in wrongs:
        with _candidate(mode):
            client, seed, db_path = _make_env(tmp_path / f"{red}-{mode}")
            try:
                with pytest.raises(DefectStillPresent):
                    check(client, seed, db_path)
            finally:
                client.close()


_CRASH_PROOF: dict[str, tuple[Any, list[str]]] = {
    "r5": (_check_r5, ["mutate_first", "reconcile_no_apply"]),
    "r6": (_check_r6, ["reconcile_no_readback"]),
    "r7": (_check_r7, ["reconcile_always_apply"]),
    "r11": (_check_r11, ["no_unique_index"]),
}


@pytest.mark.parametrize("red", sorted(_CRASH_PROOF))
def test_crash_red_proof_correct_passes_and_wrong_fails(red: str, tmp_path: Path) -> None:
    """RERUNNABLE crash-matrix evidence: a CORRECT candidate upholds the C1/C2/C3 invariant (real
    subprocess + on-disk Qdrant); each plausible-wrong candidate fails it. Candidates are test-local."""
    check, wrongs = _CRASH_PROOF[red]
    with _candidate("correct"):
        try:
            check(tmp_path / f"{red}-correct")
        except (
            DefectStillPresent
        ) as e:  # pragma: no cover - a correct candidate failing is a real defect
            raise AssertionError(f"correct candidate failed {red}: {e}") from e
    for mode in wrongs:
        with _candidate(mode), pytest.raises(DefectStillPresent):
            check(tmp_path / f"{red}-{mode}")
