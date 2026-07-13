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

This file, tranche 1: **G1** (closure-gate) + its rule-discrimination proof. The Phase-1 behavior reds
R1-R22 + G2/G3 require the coordinator/outbox + in-memory-Qdrant harness and land in the next tranche.

    uv run pytest tests/lifecycle/test_c6b_atomicity.py -v
"""

import ast
from pathlib import Path

import pytest

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
