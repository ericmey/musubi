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
import fcntl
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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
# G2 - Phase-1 acceptance guards (static AST over src): coordinator callsite inventory + cleanup SQL shape
#
# Two DEDICATED strict-xfail checkers (do NOT let one strict-xfail mask two failures). Both are RED today
# because the LifecycleTransitionCoordinator source is ABSENT (Phase 1 unbuilt); each flips to XPASS(strict)
# only when the coordinator lands with the reviewed shape. Each has its OWN DefectStillPresent reason and a
# GREEN rule-discrimination proof over temporary in-test scoped source stubs (mirroring G1's template).
# ============================================================================

_COORDINATOR_CLASS = "LifecycleTransitionCoordinator"
_COORDINATOR_METHOD = "transition"
_CLEANUP_METHOD = "cleanup_terminal"

#: The EXPLICIT Phase-1 reviewed set (a PIN, not inferred from whatever source exists): exactly ONE
#: LifecycleTransitionCoordinator.transition call, in the canonical top-level transition wrapper of
#: lifecycle/transitions.py. PATH + enclosing FUNCTION identity - NEVER a line number. H5 UPDATES this set
#: as it migrates other state-mutating paths onto the coordinator (slice-h5-unify-state-mutation).
_REVIEWED_COORDINATOR_CALLSITES: list[tuple[str, str]] = [
    ("lifecycle/transitions.py", "transition")
]


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _enclosing_func_name(parent: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    cur = parent.get(node)
    while cur is not None and not isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
        cur = parent.get(cur)
    return getattr(cur, "name", "<module>") if cur is not None else "<module>"


def _is_coordinator_ctor(expr: ast.AST) -> bool:
    """A construction of LifecycleTransitionCoordinator (by name or module.LifecycleTransitionCoordinator).
    STRUCTURAL provenance only - recognizes the coordinator by its class name AT CONSTRUCTION; there is no
    type inference here. Cross-module provenance (a coordinator received as a param from elsewhere) is out
    of scope; the reviewed set is an explicit pin (H5 updates it) rather than a full type resolution."""
    if not isinstance(expr, ast.Call):
        return False
    f = expr.func
    return (isinstance(f, ast.Name) and f.id == _COORDINATOR_CLASS) or (
        isinstance(f, ast.Attribute) and f.attr == _COORDINATOR_CLASS
    )


def _enclosing_of(
    parent: dict[ast.AST, ast.AST], node: ast.AST, types: tuple[type, ...]
) -> ast.AST | None:
    """The nearest ancestor of `node` that is an instance of one of `types` (None if there is none)."""
    cur = parent.get(node)
    while cur is not None and not isinstance(cur, types):
        cur = parent.get(cur)
    return cast("ast.AST | None", cur)


def _coordinator_scopes(
    tree: ast.AST,
) -> tuple[
    dict[ast.AST, ast.AST],
    dict[ast.AST, dict[str, list[tuple[int, bool]]]],
    dict[ast.AST, dict[str, list[bool]]],
]:
    """SCOPE-AWARE coordinator provenance (NOT module-wide) that records EVERY binding, positive or negative,
    so a REBINDING fails closed. Returns (parent_map, func_binds, class_binds):
    - func_binds: {enclosing-function node -> {local name -> [(lineno, is_coordinator_ctor), ...] for EVERY
      assignment to that name in the function}}. A local name resolves only within the function that binds
      it; a same name bound in another function is shadowed.
    - class_binds: {enclosing-class node -> {self.<attr> -> [is_coordinator_ctor, ...] for EVERY assignment
      to that attr across the class's methods}}. A self attribute resolves only within its owning class.
    Recording ALL writes (not only coordinator constructions) is what lets resolution reject a name/attr that
    is rebound to something else - the map alone no longer implies coordinator provenance."""
    parent = _parent_map(tree)
    func_binds: dict[ast.AST, dict[str, list[tuple[int, bool]]]] = {}
    class_binds: dict[ast.AST, dict[str, list[bool]]] = {}

    def _record(tgt: ast.AST, node: ast.stmt, is_ctor: bool) -> None:
        if isinstance(tgt, ast.Name):
            fn = _enclosing_of(parent, node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if fn is not None:
                func_binds.setdefault(fn, {}).setdefault(tgt.id, []).append((node.lineno, is_ctor))
        elif (
            isinstance(tgt, ast.Attribute)
            and isinstance(tgt.value, ast.Name)
            and tgt.value.id == "self"
        ):
            cls = _enclosing_of(parent, node, (ast.ClassDef,))
            if cls is not None:
                class_binds.setdefault(cls, {}).setdefault(tgt.attr, []).append(is_ctor)

    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            is_ctor = _is_coordinator_ctor(n.value)
            for tgt in n.targets:
                _record(tgt, n, is_ctor)
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            _record(n.target, n, _is_coordinator_ctor(n.value))
        elif isinstance(n, ast.AugAssign):
            _record(
                n.target, n, False
            )  # `c += ...` is a rebinding to a non-construction -> fail closed
    return parent, func_binds, class_binds


def _receiver_is_coordinator(
    recv: ast.AST,
    call_fn: ast.AST | None,
    call_cls: ast.AST | None,
    call_lineno: int,
    func_binds: dict[ast.AST, dict[str, list[tuple[int, bool]]]],
    class_binds: dict[ast.AST, dict[str, list[bool]]],
) -> bool:
    """The receiver of a `.transition` call resolves to a coordinator instance, SCOPE-AWARE and FAIL-CLOSED:
    - a local Name only if it has EXACTLY ONE binding in the enclosing function, that binding is a
      coordinator construction, and it is lexically BEFORE the call (rebinding, use-before-construct, or a
      branch/duplicate assignment all fail closed);
    - a self.<attr> only if it has EXACTLY ONE binding across the owning class and that binding is a
      coordinator construction (any rebind/ambiguity fails closed);
    - a direct `LifecycleTransitionCoordinator(...)` construction always.
    A same-named `.transition` on an unrelated object (e.g. self._plane.transition) does NOT count."""
    if isinstance(recv, ast.Name):
        if call_fn is None:
            return False
        binds = func_binds.get(call_fn, {}).get(recv.id, [])
        if len(binds) != 1:
            return False  # unbound, rebound, or branch/duplicate -> fail closed
        (lineno, is_ctor) = binds[0]
        return is_ctor and lineno < call_lineno
    if (
        isinstance(recv, ast.Attribute)
        and isinstance(recv.value, ast.Name)
        and recv.value.id == "self"
    ):
        if call_cls is None:
            return False
        attr_binds = class_binds.get(call_cls, {}).get(recv.attr, [])
        return len(attr_binds) == 1 and attr_binds[0]  # single coordinator construction, no rebind
    return _is_coordinator_ctor(recv)  # LifecycleTransitionCoordinator(...).transition(...) chain


def _coordinator_transition_calls(tree: ast.AST) -> list[ast.Call]:
    """Every `.transition(...)` Call whose receiver resolves (scope-aware, fail-closed) to the coordinator."""
    parent, func_binds, class_binds = _coordinator_scopes(tree)
    out: list[ast.Call] = []
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == _COORDINATOR_METHOD
        ):
            call_fn = _enclosing_of(parent, n, (ast.FunctionDef, ast.AsyncFunctionDef))
            call_cls = _enclosing_of(parent, n, (ast.ClassDef,))
            if _receiver_is_coordinator(
                n.func.value, call_fn, call_cls, n.lineno, func_binds, class_binds
            ):
                out.append(n)
    return out


def _resolve_callsites(tree: ast.AST, relpath: str) -> list[tuple[str, str]]:
    """(relpath, enclosing-function) for each resolved coordinator.transition call in one tree."""
    parent = _parent_map(tree)
    return [(relpath, _enclosing_func_name(parent, c)) for c in _coordinator_transition_calls(tree)]


def _scan_coordinator_transition_callsites() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for p in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        out.extend(_resolve_callsites(tree, str(p.relative_to(_SRC))))
    return out


# G2a - coordinator.transition CALLSITE INVENTORY (Phase-1 acceptance) --------------------------------- #

_G2A_REASON = (
    "the reviewed LifecycleTransitionCoordinator.transition callsite (exactly one, in the canonical "
    "transition wrapper of lifecycle/transitions.py) does not yet exist - the Phase-1 coordinator is "
    "unbuilt, so the resolved inventory is empty and cannot equal the reviewed set. Flips to XPASS(strict) "
    "when the coordinator lands and the single reviewed callsite is present (zero/missing/duplicate/extra "
    "all fail). H5 updates the reviewed set as it migrates other state-mutating paths."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_G2A_REASON)
def test_g2a_coordinator_transition_callsite_inventory() -> None:
    """Phase-1 acceptance: the resolved set of LifecycleTransitionCoordinator.transition callsites in src
    must equal the EXPLICIT reviewed set - fails on zero, missing, duplicate, or extra."""
    found = sorted(_scan_coordinator_transition_callsites())
    reviewed = sorted(_REVIEWED_COORDINATOR_CALLSITES)
    if found != reviewed:
        raise DefectStillPresent(
            f"coordinator.transition callsite inventory {found} != reviewed set {reviewed} "
            "(zero/missing/duplicate/extra)"
        )


def test_g2a_rule_discriminates_coordinator_callsites() -> None:
    """GREEN mechanism proof: SCOPE-AWARE receiver-provenance distinguishes the coordinator from unrelated
    same-named .transition methods - a local name only inside the function that binds it, a self attr only
    inside its owning class - and the inventory comparison fails on missing/duplicate/extra."""
    reviewed = [("m", "transition")]
    # construct-in-a-method + call-in-a-method, resolved via the owning class's self attribute
    ctor = "    def build(self):\n        self._c = LifecycleTransitionCoordinator(client=a, db_path=b)\n"
    exact = ast.parse(
        "class W:\n" + ctor + "    def transition(self, i):\n        return self._c.transition(i)\n"
    )
    unrelated = ast.parse(
        "class W:\n    def transition(self, i):\n        return self._plane.transition(i)\n"
    )
    missing = ast.parse("class W:\n" + ctor + "    def transition(self, i):\n        return None\n")
    extra = ast.parse(
        "class W:\n"
        + ctor
        + "    def transition(self, i):\n        return self._c.transition(i)\n"
        + "    def sneak(self, i):\n        return self._c.transition(i)\n"
    )
    duplicate = ast.parse(
        "class W:\n"
        + ctor
        + "    def transition(self, i):\n        self._c.transition(i)\n        return self._c.transition(i)\n"
    )
    chained = ast.parse(
        "def transition(self, i):\n    return LifecycleTransitionCoordinator(client=a).transition(i)\n"
    )
    # a local name bound to the coordinator IN THE SAME function resolves
    local_ok = ast.parse(
        "def transition(self, i):\n    c = LifecycleTransitionCoordinator(client=a)\n    return c.transition(i)\n"
    )
    # SCOPE-AWARE: a same local name bound to something UNRELATED in another function is shadowed/ignored
    shadowed = ast.parse(
        "def build(self):\n    c = LifecycleTransitionCoordinator(client=a)\n    return c\n"
        "def other(self, i):\n    c = make_plane()\n    return c.transition(i)\n"
    )
    # SCOPE-AWARE: the SAME self attr name on a DIFFERENT (non-coordinator) class is ignored
    attr_other_class = ast.parse(
        "class Coord:\n"
        "    def build(self):\n        self._c = LifecycleTransitionCoordinator(client=a, db_path=b)\n"
        "    def transition(self, i):\n        return self._c.transition(i)\n"
        "class Other:\n"
        "    def build(self):\n        self._c = make_plane()\n"
        "    def transition(self, i):\n        return self._c.transition(i)\n"
    )
    # a same-named UNRELATED transition does not count
    assert _resolve_callsites(unrelated, "m") == []
    # the EXACT reviewed set passes; a direct constructor-chain and a same-function local both resolve
    assert sorted(_resolve_callsites(exact, "m")) == reviewed
    assert sorted(_resolve_callsites(chained, "m")) == reviewed
    assert sorted(_resolve_callsites(local_ok, "m")) == reviewed
    # a MISSING real callsite fails (inventory != reviewed)
    assert sorted(_resolve_callsites(missing, "m")) != reviewed
    # an EXTRA real callsite fails
    assert sorted(_resolve_callsites(extra, "m")) != reviewed
    # a DUPLICATE (two coordinator calls in the reviewed function) fails on multiplicity
    assert sorted(_resolve_callsites(duplicate, "m")) != reviewed
    assert len(_resolve_callsites(duplicate, "m")) == 2
    # a shadowed local name in an unrelated function is IGNORED
    assert _resolve_callsites(shadowed, "m") == []
    # only the coordinator class's self._c.transition counts; the other class's identical attr is ignored
    assert sorted(_resolve_callsites(attr_other_class, "m")) == [("m", "transition")]
    # FAIL-CLOSED: a same-function local REBOUND to a non-coordinator does NOT count
    local_rebind = ast.parse(
        "def transition(self, i):\n"
        "    c = LifecycleTransitionCoordinator(client=a)\n"
        "    c = make_plane()\n"
        "    return c.transition(i)\n"
    )
    assert _resolve_callsites(local_rebind, "m") == []
    # FAIL-CLOSED: a self attr bound to the coordinator in one method, REBOUND in another, does NOT count
    attr_rebind = ast.parse(
        "class W:\n"
        "    def build(self):\n        self._c = LifecycleTransitionCoordinator(client=a, db_path=b)\n"
        "    def rebuild(self):\n        self._c = make_plane()\n"
        "    def transition(self, i):\n        return self._c.transition(i)\n"
    )
    assert _resolve_callsites(attr_rebind, "m") == []
    # FAIL-CLOSED: a call lexically BEFORE the (single) constructor binding does NOT count
    use_before_construct = ast.parse(
        "def transition(self, i):\n"
        "    x = c.transition(i)\n"
        "    c = LifecycleTransitionCoordinator(client=a)\n"
        "    return x\n"
    )
    assert _resolve_callsites(use_before_construct, "m") == []
    # FAIL-CLOSED: a branch-ambiguous binding (coordinator in one arm, other in the other) does NOT count
    branch_ambiguous = ast.parse(
        "def transition(self, i):\n"
        "    if flag:\n        c = LifecycleTransitionCoordinator(client=a)\n"
        "    else:\n        c = make_plane()\n"
        "    return c.transition(i)\n"
    )
    assert _resolve_callsites(branch_ambiguous, "m") == []


# G2b - cleanup_terminal SQL SHAPE (Phase-1 source-shape acceptance) ----------------------------------- #
#
# A BOUNDED TOKENIZER + token-SEQUENCE matching over the ACTUAL SQL string argument passed to the DB call
# inside cleanup_terminal - NOT substring matching and NOT a full SQL parser. It tokenizes with real
# identifier boundaries, skips comments, treats string literals and quoted identifiers as opaque (their
# contents never satisfy a keyword), validates balanced parentheses, and matches exact token runs for the
# pinned shape. If the supported shape cannot be recognized unambiguously, it fails closed.


def _static_str(node: ast.AST) -> str | None:
    """The static string value of a literal / `+`-concatenation / f-string constant parts (None if it is
    a non-static dynamic expression)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_str(node.left), _static_str(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else " "
            for v in node.values
        )
    return None


def _coordinator_cleanup_methods(
    tree: ast.AST,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The cleanup_terminal method nodes that are DIRECT members of a LifecycleTransitionCoordinator class
    body - so an unrelated class's same-named cleanup_terminal does NOT count."""
    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == _COORDINATOR_CLASS:
            for item in n.body:
                if (
                    isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                    and item.name == _CLEANUP_METHOD
                ):
                    out.append(item)
    return out


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _same_scope_nodes(node: ast.AST) -> list[ast.AST]:
    """All descendant nodes of `node` that share its lexical scope - descending into control-flow blocks
    (if/for/while/with/try) but NOT into nested function/class/lambda scopes (whose statements belong to a
    different scope and must not be attributed to this method)."""
    out: list[ast.AST] = []

    def _visit(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            out.append(child)
            if not isinstance(child, _NESTED_SCOPES):
                _visit(child)

    _visit(node)
    return out


def _method_sql_args(method: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Every SQL string argument passed to a `.execute`/`.executemany` call in the method's OWN lexical scope
    (nested def/async-def/lambda/class bodies are EXCLUDED). A first argument that is a LOCAL NAME is resolved
    ONLY when the name has exactly ONE static assignment that is a DIRECT statement of the method body (not
    inside a branch/loop/nested scope) and is lexically BEFORE the execute. Reassignment, use-before-assign,
    branch/nested-only assignment, or a dynamic value all FAIL CLOSED as a `<DYNAMIC>` sentinel so the shape
    check cannot be fooled rather than silently vanishing."""
    same_scope = _same_scope_nodes(method)
    # every same-scope assignment target name -> count (any 2nd write, in ANY branch, is ambiguity)
    write_counts: dict[str, int] = {}
    for n in same_scope:
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    write_counts[tgt.id] = write_counts.get(tgt.id, 0) + 1
        elif isinstance(n, ast.AnnAssign | ast.AugAssign) and isinstance(n.target, ast.Name):
            write_counts[n.target.id] = write_counts.get(n.target.id, 0) + 1
    # static single assignments that are DIRECT statements of the method body (top-level control scope)
    top_static: dict[str, tuple[int, str]] = {}
    for stmt in method.body:
        name: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            name, value = stmt.targets[0].id, stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            name, value = stmt.target.id, stmt.value
        if name is not None and value is not None:
            s = _static_str(value)
            if s is not None:
                top_static[name] = (stmt.lineno, s)
    out: list[str] = []
    for c in same_scope:
        if (
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr in ("execute", "executemany")
            and c.args
        ):
            a0 = c.args[0]
            s = _static_str(a0)
            if s is not None:
                out.append(s)
            elif (
                isinstance(a0, ast.Name)
                and a0.id in top_static
                and write_counts.get(a0.id, 0) == 1
                and top_static[a0.id][0] < c.lineno
            ):
                out.append(top_static[a0.id][1])
            else:
                out.append("<DYNAMIC>")
    return out


def _scan_cleanup_terminal_sql() -> tuple[int, list[str]]:
    """(count, sql_args) for the UNIQUE LifecycleTransitionCoordinator.cleanup_terminal method across src:
    count==0 -> unbuilt (Phase-1 coordinator absent); count>1 -> a dedicated ambiguity violation; count==1
    -> that one method's resolved SQL arguments."""
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for p in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        methods.extend(_coordinator_cleanup_methods(tree))
    if len(methods) == 1:
        return 1, _method_sql_args(methods[0])
    return len(methods), []


_TERMINAL_STATE_VALUES = {
    "FINAL",
    "ABANDONED",
}  #: the ONLY string literals the pinned shape may contain

_Tok = tuple[str, str]  #: (kind, value) - kind in WORD/STR/QID/NUM/PARAM/PUNCT


def _sql_tokens(raw: str) -> list[_Tok] | None:
    """A BOUNDED SQLite-ish TOKENIZER (NOT a full parser) with real identifier boundaries. Emits (kind,
    value) tokens:
      WORD  a bare identifier/keyword, uppercased - boundary-delimited, so SOMEWITH / NOTRETURNING / LIMITX
            are each ONE word and never the WITH / RETURNING / LIMIT keyword;
      STR   a single-quoted string literal value, uppercased ('' escape handled);
      QID   a quoted identifier ("x" / `x` / [x]), uppercased - its content NEVER matches a keyword;
      NUM   a digit run;  PARAM  a bound parameter (:name / ? / @name / $name);
      PUNCT one significant punctuation char, e.g. ( ) , ; < > = . *
    `--` line and `/* */` block comments are skipped. Returns None (fail closed) on an unterminated string,
    block comment, or quoted identifier."""
    toks: list[_Tok] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        pair = raw[i : i + 2]
        if pair == "--":
            j = raw.find("\n", i)
            i = n if j < 0 else j + 1
        elif pair == "/*":
            j = raw.find("*/", i + 2)
            if j < 0:
                return None
            i = j + 2
        elif ch.isspace():
            i += 1
        elif ch == "'":
            i, val, closed = i + 1, [], False
            while i < n:
                if raw[i] == "'":
                    if i + 1 < n and raw[i + 1] == "'":
                        val.append("'")
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                val.append(raw[i])
                i += 1
            if not closed:
                return None
            toks.append(("STR", "".join(val).upper()))
        elif ch in ('"', "`"):
            q = ch
            i, val, closed = i + 1, [], False
            while i < n:
                if raw[i] == q:
                    if i + 1 < n and raw[i + 1] == q:
                        val.append(q)
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                val.append(raw[i])
                i += 1
            if not closed:
                return None
            toks.append(("QID", "".join(val).upper()))
        elif ch == "[":
            j = raw.find("]", i + 1)
            if j < 0:
                return None
            toks.append(("QID", raw[i + 1 : j].upper()))
            i = j + 1
        elif ch.isdigit():
            j = i
            while j < n and raw[j].isdigit():
                j += 1
            toks.append(("NUM", raw[i:j]))
            i = j
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (raw[j].isalnum() or raw[j] == "_"):
                j += 1
            toks.append(("WORD", raw[i:j].upper()))
            i = j
        elif ch in ":@$":
            j = i + 1
            while j < n and (raw[j].isalnum() or raw[j] == "_"):
                j += 1
            toks.append(("PARAM", raw[i:j]))
            i = j
        elif ch == "?":
            toks.append(("PARAM", "?"))
            i += 1
        else:
            toks.append(("PUNCT", ch))
            i += 1
    return toks


def _split_top_statements(toks: list[_Tok]) -> list[list[_Tok]]:
    """Split a token list on top-level `;` (a `;` is always a PUNCT token, never inside a string/comment),
    dropping empty (whitespace/comment-only) statements."""
    stmts: list[list[_Tok]] = []
    cur: list[_Tok] = []
    for t in toks:
        if t == ("PUNCT", ";"):
            stmts.append(cur)
            cur = []
        else:
            cur.append(t)
    stmts.append(cur)
    return [s for s in stmts if s]


def _paren_balanced(toks: list[_Tok]) -> bool:
    depth = 0
    for t in toks:
        if t == ("PUNCT", "("):
            depth += 1
        elif t == ("PUNCT", ")"):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _has_seq(toks: list[_Tok], seq: list[_Tok]) -> bool:
    """True if `seq` appears as a CONTIGUOUS run of tokens in `toks` (exact (kind,value) match)."""
    m = len(seq)
    return any(toks[i : i + m] == seq for i in range(len(toks) - m + 1))


def _split_cte_tokens(
    toks: list[_Tok],
) -> tuple[str | None, list[_Tok] | None, list[_Tok] | None]:
    """(cte_name, inner_tokens, outer_tokens) for a `WITH <name> AS ( ... ) <outer>` head, matching the
    CTE's balanced parens so nested `(...)` stays with the inner. (None, None, None) if the head is absent
    or its parens are unbalanced."""
    for i in range(len(toks) - 3):
        if (
            toks[i] == ("WORD", "WITH")
            and toks[i + 1][0] == "WORD"
            and toks[i + 2] == ("WORD", "AS")
            and toks[i + 3] == ("PUNCT", "(")
        ):
            name = toks[i + 1][1]
            depth, k = 0, i + 3
            while k < len(toks):
                if toks[k] == ("PUNCT", "("):
                    depth += 1
                elif toks[k] == ("PUNCT", ")"):
                    depth -= 1
                    if depth == 0:
                        return name, toks[i + 4 : k], toks[k + 1 :]
                k += 1
            return None, None, None
    return None, None, None


def _has_terminal_state(toks: list[_Tok]) -> bool:
    """`state IN ('FINAL','ABANDONED')` as an exact token sequence with the two terminal string values."""
    for i in range(len(toks) - 6):
        if (
            toks[i] == ("WORD", "STATE")
            and toks[i + 1] == ("WORD", "IN")
            and toks[i + 2] == ("PUNCT", "(")
            and toks[i + 3][0] == "STR"
            and toks[i + 4] == ("PUNCT", ",")
            and toks[i + 5][0] == "STR"
            and toks[i + 6] == ("PUNCT", ")")
            and {toks[i + 3][1], toks[i + 5][1]} == _TERMINAL_STATE_VALUES
        ):
            return True
    return False


def _cleanup_sql_violations(sql_args: list[str]) -> list[str]:
    """Bounded-PARSER shape violations of the cleanup DELETE (a real tokenizer + token-SEQUENCE matching,
    NOT substring matching and NOT a full SQL parser; fails closed when the supported shape cannot be
    recognized unambiguously). [] = the pinned atomic shape: ONE executable statement whose parentheses are
    balanced; a `WITH <sel>` CTE whose inner selector is ORDER BY terminal_epoch THEN operation_key with a
    bounded LIMIT; an outer DELETE restricted to `operation_key IN (SELECT operation_key FROM <sel>)`; the
    FULL terminal-eligibility predicate - state IN ('FINAL','ABANDONED') AND terminal_epoch IS NOT NULL AND
    terminal_epoch < <cutoff> - as exact token runs in BOTH halves; RETURNING; and NO other statement after
    it. Comments, string contents, and quoted identifiers never satisfy a keyword; a string literal other
    than the terminal state values, or unbalanced parens, fail closed. Count SELECTs are ignored."""
    v: list[str] = []
    tokenized = [_sql_tokens(raw) for raw in sql_args]
    if any(t is None for t in tokenized):
        return ["unlexable_sql"]  # unterminated string/comment/quoted-id -> fail closed
    delete_args: list[list[list[_Tok]]] = []
    batch_selectors: list[list[list[_Tok]]] = []
    for toks in tokenized:
        assert toks is not None
        stmts = _split_top_statements(toks)
        has_delete = any(("WORD", "DELETE") in s for s in stmts)
        has_opkey = any(("WORD", "OPERATION_KEY") in s for s in stmts)
        has_limit = any(("WORD", "LIMIT") in s for s in stmts)
        if has_delete:
            delete_args.append(stmts)
        elif has_opkey and has_limit:
            batch_selectors.append(stmts)
    if batch_selectors and delete_args:
        v.append(
            "split_select_delete"
        )  # a standalone batch SELECT + a separate DELETE (two statements)
    if len(delete_args) != 1:
        v.append("not_single_delete")
        return v  # cannot shape-check the delete without exactly one
    stmts = delete_args[0]
    if len(stmts) != 1:
        v.append("multiple_statements")  # trailing statement after the single CTE DELETE
        return v
    toks = stmts[0]
    if not _paren_balanced(toks):
        v.append("unbalanced_parens")
        return v
    if any(val not in _TERMINAL_STATE_VALUES for kind, val in toks if kind == "STR"):
        v.append("unexpected_string_literal")  # a keyword/value smuggled in a string -> fail closed
        return v
    name, inner, outer = _split_cte_tokens(toks)
    if name is None or inner is None or outer is None:
        v.append("missing_cte")
        return v
    if ("WORD", "RETURNING") not in outer:
        v.append("missing_returning")
    if ("WORD", "LIMIT") not in inner:
        v.append("missing_bound")
    tie = [
        ("WORD", "ORDER"),
        ("WORD", "BY"),
        ("WORD", "TERMINAL_EPOCH"),
        ("PUNCT", ","),
        ("WORD", "OPERATION_KEY"),
    ]
    if not _has_seq(inner, tie):
        v.append("nondeterministic_tie")  # ORDER BY missing the operation_key tiebreak
    restrict = [
        ("WORD", "OPERATION_KEY"),
        ("WORD", "IN"),
        ("PUNCT", "("),
        ("WORD", "SELECT"),
        ("WORD", "OPERATION_KEY"),
        ("WORD", "FROM"),
        ("WORD", name),
        ("PUNCT", ")"),
    ]
    if not _has_seq(outer, restrict):
        v.append("delete_not_restricted")  # DELETE not restricted to the selected operation keys
    notnull = [("WORD", "TERMINAL_EPOCH"), ("WORD", "IS"), ("WORD", "NOT"), ("WORD", "NULL")]
    age = [("WORD", "TERMINAL_EPOCH"), ("PUNCT", "<")]
    for half, body in (("inner", inner), ("outer", outer)):
        if not _has_terminal_state(body):
            v.append(f"missing_{half}_state")
        if not _has_seq(body, notnull):
            v.append(f"missing_{half}_notnull")
        if not _has_seq(body, age):
            v.append(f"missing_{half}_age")
    return v


_G2B_CORRECT = (
    "WITH sel AS (SELECT operation_key FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED') AND "
    "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff ORDER BY terminal_epoch, operation_key "
    "LIMIT :batch) DELETE FROM lifecycle_outbox WHERE operation_key IN (SELECT operation_key FROM sel) "
    "AND state IN ('FINAL','ABANDONED') AND terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff "
    "RETURNING operation_key"
)

_G2B_REASON = (
    "the unique LifecycleTransitionCoordinator.cleanup_terminal is not implemented in src (Phase-1 "
    "coordinator unbuilt), so its SQL shape cannot be accepted. When it lands it must be ONE atomic "
    "statement: a WITH <sel> CTE ordered by terminal_epoch THEN operation_key with a bounded LIMIT, a "
    "DELETE restricted to the selected operation keys, the FULL terminal-eligibility predicate (state IN "
    "('FINAL','ABANDONED') AND terminal_epoch IS NOT NULL AND terminal_epoch < cutoff) repeated in BOTH the "
    "inner selector AND the outer DELETE WHERE, RETURNING, and NO other statement after it. Recognition is "
    "via a bounded tokenizer with real identifier boundaries + exact token-SEQUENCE matching (comments, "
    "string contents, and quoted identifiers never satisfy a keyword; parentheses must balance; a "
    "non-terminal string literal fails closed), not substring matching. Zero or multiple coordinator cleanup "
    "methods, a split select/delete, a trailing statement, unbalanced parens, any missing inner/outer "
    "state/non-null/age component, a missing tiebreak/LIMIT/RETURNING, or a dynamic/unresolvable SQL "
    "argument each fail."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_G2B_REASON)
def test_g2b_cleanup_terminal_sql_shape() -> None:
    """Phase-1 source-shape acceptance: the UNIQUE LifecycleTransitionCoordinator.cleanup_terminal method's
    actual SQL argument (with a local one-static-assignment `sql = ...` resolved) must be the pinned atomic
    CTE-DELETE shape (token-aware inspection, not a full SQL parse)."""
    count, sql_args = _scan_cleanup_terminal_sql()
    if count == 0:
        raise DefectStillPresent(
            "the LifecycleTransitionCoordinator.cleanup_terminal method is not implemented in src (the "
            "Phase-1 coordinator is unbuilt)"
        )
    if count > 1:
        raise DefectStillPresent(
            f"{count} LifecycleTransitionCoordinator.cleanup_terminal methods in src - expected exactly one"
        )
    violations = _cleanup_sql_violations(sql_args)
    if violations:
        raise DefectStillPresent(f"cleanup_terminal SQL shape violations: {sorted(violations)}")


def test_g2b_rule_discriminates_cleanup_sql_shape() -> None:
    """GREEN mechanism proof (two layers): (1) token-aware violations - the pinned shape passes; a split
    select/delete, a nondeterministic tie, a missing LIMIT/RETURNING, and EACH of the three eligibility
    components (state / non-null / age) missing from EITHER half fail INDEPENDENTLY at their intended code;
    (2) through the ACTUAL method scanner - only the unique LifecycleTransitionCoordinator.cleanup_terminal
    is inspected (an unrelated class's same-named method is ignored, zero/multiple are non-unique), and a
    local `sql = ...` variable is resolved while a reassigned/dynamic argument fails closed."""
    inner_sel = (
        "SELECT operation_key FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED') AND "
        "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff ORDER BY terminal_epoch, operation_key "
        "LIMIT :batch"
    )
    split_delete = (
        "DELETE FROM lifecycle_outbox WHERE operation_key IN (:keys) AND state IN ('FINAL','ABANDONED') "
        "AND terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff RETURNING operation_key"
    )
    # each eligibility COMPONENT dropped from ONE half, surgically (suffix anchors keep it half-specific)
    missing_inner_state = _G2B_CORRECT.replace(
        "WHERE state IN ('FINAL','ABANDONED') AND terminal_epoch IS NOT NULL",
        "WHERE terminal_epoch IS NOT NULL",
    )
    missing_outer_state = _G2B_CORRECT.replace(
        "FROM sel) AND state IN ('FINAL','ABANDONED') AND terminal_epoch IS NOT NULL",
        "FROM sel) AND terminal_epoch IS NOT NULL",
    )
    missing_inner_notnull = _G2B_CORRECT.replace(
        "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff ORDER BY",
        "terminal_epoch < :cutoff ORDER BY",
    )
    missing_outer_notnull = _G2B_CORRECT.replace(
        "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff RETURNING",
        "terminal_epoch < :cutoff RETURNING",
    )
    missing_inner_age = _G2B_CORRECT.replace(
        "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff ORDER BY",
        "terminal_epoch IS NOT NULL ORDER BY",
    )
    missing_outer_age = _G2B_CORRECT.replace(
        "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff RETURNING",
        "terminal_epoch IS NOT NULL RETURNING",
    )
    nondeterministic = _G2B_CORRECT.replace(
        "ORDER BY terminal_epoch, operation_key", "ORDER BY terminal_epoch"
    )
    missing_bound = _G2B_CORRECT.replace(" LIMIT :batch", "")
    missing_returning = _G2B_CORRECT.replace(" RETURNING operation_key", "")

    assert _cleanup_sql_violations([_G2B_CORRECT]) == []
    # the count SELECTs must not be mistaken for a split (no operation_key + no LIMIT)
    assert (
        _cleanup_sql_violations(
            [
                _G2B_CORRECT,
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED')",
            ]
        )
        == []
    )
    assert "split_select_delete" in _cleanup_sql_violations([inner_sel, split_delete])
    assert _cleanup_sql_violations([missing_inner_state]) == ["missing_inner_state"]
    assert _cleanup_sql_violations([missing_outer_state]) == ["missing_outer_state"]
    assert _cleanup_sql_violations([missing_inner_notnull]) == ["missing_inner_notnull"]
    assert _cleanup_sql_violations([missing_outer_notnull]) == ["missing_outer_notnull"]
    assert _cleanup_sql_violations([missing_inner_age]) == ["missing_inner_age"]
    assert _cleanup_sql_violations([missing_outer_age]) == ["missing_outer_age"]
    assert _cleanup_sql_violations([nondeterministic]) == ["nondeterministic_tie"]
    assert _cleanup_sql_violations([missing_bound]) == ["missing_bound"]
    assert _cleanup_sql_violations([missing_returning]) == ["missing_returning"]

    # ---- bounded lexer: comments/strings never satisfy a keyword; exactly ONE executable statement ----
    # a trailing SELECT or DELETE after the single CTE DELETE is rejected (not one atomic statement)
    assert "multiple_statements" in _cleanup_sql_violations([_G2B_CORRECT + "; SELECT 1"])
    assert "multiple_statements" in _cleanup_sql_violations(
        [_G2B_CORRECT + "; DELETE FROM unrelated"]
    )
    # RETURNING only inside a `--` comment is NOT a RETURNING keyword -> missing_returning
    comment_returning = _G2B_CORRECT.replace(
        "RETURNING operation_key", "-- RETURNING operation_key"
    )
    assert _cleanup_sql_violations([comment_returning]) == ["missing_returning"]
    # an eligibility token present ONLY in a comment does not count (here the outer age)
    comment_outer_age = missing_outer_age + " -- and terminal_epoch < :cutoff on the outer"
    assert _cleanup_sql_violations([comment_outer_age]) == ["missing_outer_age"]
    # a keyword hidden in a STRING literal is not a keyword, and the non-terminal literal fails closed
    string_returning = _G2B_CORRECT.replace("RETURNING operation_key", "AND note = 'RETURNING'")
    assert _cleanup_sql_violations([string_returning]) == ["unexpected_string_literal"]
    # an unterminated string/comment fails closed
    assert _cleanup_sql_violations([_G2B_CORRECT + " /* unterminated"]) == ["unlexable_sql"]

    # ---- token BOUNDARIES: a substring of a keyword is a DIFFERENT word; quoted ids never match ----
    # RETURNING as a substring of another identifier is not the keyword -> missing_returning
    assert _cleanup_sql_violations(
        [_G2B_CORRECT.replace("RETURNING operation_key", "AND NOTRETURNING = 1")]
    ) == ["missing_returning"]
    # RETURNING as a quoted IDENTIFIER (double-quote / backtick / bracket) is not the keyword
    for q_open, q_close in (('"', '"'), ("`", "`"), ("[", "]")):
        quoted = _G2B_CORRECT.replace(
            "RETURNING operation_key", f"AND {q_open}RETURNING{q_close} = 1"
        )
        assert _cleanup_sql_violations([quoted]) == ["missing_returning"]
    # LIMIT / WITH / operation_key as a substring of a longer identifier are different words
    assert _cleanup_sql_violations(
        [_G2B_CORRECT.replace(" LIMIT :batch", " AND LIMITX = :batch")]
    ) == ["missing_bound"]
    assert _cleanup_sql_violations([_G2B_CORRECT.replace("WITH sel AS", "SOMEWITH sel AS")]) == [
        "missing_cte"
    ]
    assert _cleanup_sql_violations(
        [
            _G2B_CORRECT.replace(
                "ORDER BY terminal_epoch, operation_key",
                "ORDER BY terminal_epoch, operation_key_suffix",
            )
        ]
    ) == ["nondeterministic_tie"]
    # an unmatched ')' after the CTE is malformed structure -> fail closed
    assert _cleanup_sql_violations([_G2B_CORRECT + ")"]) == ["unbalanced_parens"]

    # CONTROLS: harmless comments, a bare `;` terminator, and a LEGALLY quoted non-structural identifier
    # (the table name) with correct balanced nesting all keep the correct shape green
    assert _cleanup_sql_violations(["-- cleanup terminal rows\n" + _G2B_CORRECT + " -- done"]) == []
    assert _cleanup_sql_violations([_G2B_CORRECT + " ;  "]) == []
    assert (
        _cleanup_sql_violations([_G2B_CORRECT.replace("lifecycle_outbox", '"lifecycle_outbox"')])
        == []
    )

    # ---- through the ACTUAL method scanner (unique coordinator method + local SQL var resolution) ----
    def scan(src: str) -> tuple[int, list[str]]:
        methods = _coordinator_cleanup_methods(ast.parse(src))
        return len(methods), (_method_sql_args(methods[0]) if len(methods) == 1 else [])

    correct_method = (
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        f"        sql = {_G2B_CORRECT!r}\n"
        "        con.execute(sql, params)\n"
    )
    n, args = scan(correct_method)
    assert n == 1 and _cleanup_sql_violations(args) == []  # local `sql = <static>` resolved
    # an unrelated class's same-named cleanup_terminal is IGNORED (only the coordinator class counts)
    n2, args2 = scan(
        correct_method
        + "class Other:\n    def cleanup_terminal(self):\n        con.execute('DELETE FROM whatever', [])\n"
    )
    assert n2 == 1 and _cleanup_sql_violations(args2) == []
    # a REASSIGNED sql var fails closed as <DYNAMIC>
    n3, args3 = scan(
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        f"        sql = {_G2B_CORRECT!r}\n"
        "        sql = sql + ' -- tweak'\n"
        "        con.execute(sql, params)\n"
    )
    assert n3 == 1 and args3 == ["<DYNAMIC>"] and _cleanup_sql_violations(args3) != []
    # a fully DYNAMIC sql argument fails closed as <DYNAMIC>
    n4, args4 = scan(
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        "        con.execute(build_sql(self.flag), params)\n"
    )
    assert n4 == 1 and args4 == ["<DYNAMIC>"] and _cleanup_sql_violations(args4) != []
    # FAIL-CLOSED (order): execute BEFORE the sql assignment (use-before-assign) does not resolve
    n5, args5 = scan(
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        "        con.execute(sql, params)\n"
        f"        sql = {_G2B_CORRECT!r}\n"
    )
    assert n5 == 1 and args5 == ["<DYNAMIC>"] and _cleanup_sql_violations(args5) != []
    # FAIL-CLOSED (scope): sql assignment + execute live ONLY in a nested helper def -> not the method's
    n6, args6 = scan(
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        "        def helper():\n"
        f"            sql = {_G2B_CORRECT!r}\n"
        "            con.execute(sql, params)\n"
        "        helper()\n"
    )
    assert n6 == 1 and args6 == [] and _cleanup_sql_violations(args6) != []
    # FAIL-CLOSED (branch): a single assignment nested inside a branch is not a direct-body binding
    n7, args7 = scan(
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        "        if flag:\n"
        f"            sql = {_G2B_CORRECT!r}\n"
        "        con.execute(sql, params)\n"
    )
    assert n7 == 1 and args7 == ["<DYNAMIC>"] and _cleanup_sql_violations(args7) != []
    # FAIL-CLOSED (multiple deletes): two DELETE executes cannot be shape-checked as one
    n8, args8 = scan(
        "class LifecycleTransitionCoordinator:\n"
        "    def cleanup_terminal(self):\n"
        f"        con.execute({_G2B_CORRECT!r}, params)\n"
        f"        con.execute({_G2B_CORRECT!r}, params)\n"
    )
    assert n8 == 1 and "not_single_delete" in _cleanup_sql_violations(args8)
    # zero coordinator cleanup methods (unbuilt) and multiple (ambiguous) are both non-unique
    assert scan("class Other:\n    def cleanup_terminal(self):\n        pass\n")[0] == 0
    assert scan(correct_method + correct_method)[0] == 2


# ============================================================================
# G3 - Phase-1 acceptance guard (static AST over src): the coordinator TransitionOutcome is consumed
#
# Reuses G2a's receiver-provenance resolution (_coordinator_transition_calls) and REJECTS a bare-expression
# result - a coordinator.transition(...) call as a bare expression statement, its three-way Result dropped.
# Assignment, return, a match subject, or deliberate argument forwarding all COUNT as consumed. G3 proves
# only that the Result is not silently dropped; it does NOT prove the stronger three-way business branching
# (Final vs Pending vs Err) - that stays in the R21 behavior red. RED today (source absent); XPASS(strict)
# when the reviewed callsite lands and consumes its Result.
# ============================================================================


def _all_discard(target: ast.AST) -> bool:
    """A recursively all-underscore assignment target: the bare name `_`, or a tuple/list/starred form whose
    every leaf is `_` (`(_,)`, `[_, _]`, `(*_,)`). Any real name, attribute, or subscript leaf means a real
    sink receives (part of) the value -> NOT a discard."""
    if isinstance(target, ast.Name):
        return target.id == "_"
    if isinstance(target, ast.Starred):
        return _all_discard(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return len(target.elts) > 0 and all(_all_discard(e) for e in target.elts)
    return False


def _call_result_dropped(parent: dict[ast.AST, ast.AST], call: ast.Call) -> bool:
    """A coordinator.transition Call whose Result is dropped: (1) a bare Expr statement, or (2) an
    assignment/unpacking in which EVERY bound target recursively resolves to the discard name `_` (`_ = ...`,
    `_: X = ...`, `(_,) = ...`, `[_, _] = ...`, `(*_,) = ...`) - all explicitly throw the three-way
    TransitionOutcome away. A NAMED Assign/AnnAssign value, a Return value, a Match subject, an argument to
    another call, or ANY multi-target/unpack where a real name receives (part of) the value all consume it."""
    p = parent.get(call)
    if isinstance(p, ast.Expr):
        return True
    if isinstance(p, ast.Assign):
        return all(_all_discard(t) for t in p.targets)
    if isinstance(p, ast.AnnAssign):
        return _all_discard(p.target)
    return False


def _scan_coordinator_transition_consumption() -> list[tuple[str, str, bool]]:
    """(relpath, enclosing-function, dropped) for each resolved coordinator.transition callsite in src."""
    out: list[tuple[str, str, bool]] = []
    for p in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        parent = _parent_map(tree)
        for c in _coordinator_transition_calls(tree):
            out.append(
                (
                    str(p.relative_to(_SRC)),
                    _enclosing_func_name(parent, c),
                    _call_result_dropped(parent, c),
                )
            )
    return out


_G3_REASON = (
    "the reviewed LifecycleTransitionCoordinator.transition callsite does not yet exist (Phase-1 "
    "coordinator unbuilt), so there is no resolved call whose three-way TransitionOutcome could be "
    "verified as consumed. Flips to XPASS(strict) when the callsite lands and its Result is consumed "
    "(NAMED assignment / return / match subject / argument forwarding); a bare-expression result OR an "
    "assignment/unpack whose every target is the discard name `_` (`_ = ...` / `_: X = ...` / `(_,) = ...` / "
    "`[_, _] = ...` / `(*_,) = ...`) fails. G3 does NOT prove the three-way Final/Pending/Err branching - "
    "that stays in the R21 behavior red."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_G3_REASON)
def test_g3_coordinator_transition_result_consumed() -> None:
    """Phase-1 acceptance: no resolved coordinator.transition callsite may drop its Result (a bare
    expression statement). RED today - there is no callsite yet to verify."""
    sites = _scan_coordinator_transition_consumption()
    if not sites:
        raise DefectStillPresent(
            "no resolved coordinator.transition callsite to verify (the Phase-1 coordinator is unbuilt)"
        )
    dropped = [(f, fn) for f, fn, d in sites if d]
    if dropped:
        raise DefectStillPresent(
            f"coordinator.transition result dropped as a bare expression at {sorted(dropped)} "
            "(assign/return/match/forward the three-way TransitionOutcome)"
        )


def test_g3_rule_discriminates_result_consumed() -> None:
    """GREEN mechanism proof: an UNRELATED same-named .transition bare expression is IGNORED; a REAL
    coordinator bare expression AND an assignment to the discard target `_` (`_ = ...` / `_: X = ...`) are
    dropped; each legitimate consumed form (named assign / return / match subject / arg-forward, and a
    multi-target assign that also captures under a real name) passes. Reuses G2a's scope-aware provenance."""

    def dropped_flags(src: str) -> list[bool]:
        tree = ast.parse(src)
        parent = _parent_map(tree)
        return [_call_result_dropped(parent, c) for c in _coordinator_transition_calls(tree)]

    head = (
        "class W:\n"
        "    def build(self):\n        self._c = LifecycleTransitionCoordinator(client=a, db_path=b)\n"
    )
    unrelated = (
        "class W:\n    def f(self, i):\n        self._plane.transition(i)\n"  # unrelated object
    )
    bare = head + "    def f(self, i):\n        self._c.transition(i)\n"
    discard = head + "    def f(self, i):\n        _ = self._c.transition(i)\n"
    discard_ann = head + "    def f(self, i):\n        _: object = self._c.transition(i)\n"
    assign = head + "    def f(self, i):\n        r = self._c.transition(i)\n        return r\n"
    returned = head + "    def f(self, i):\n        return self._c.transition(i)\n"
    matched = (
        head
        + "    def f(self, i):\n        match self._c.transition(i):\n"
        + "            case _:\n                return None\n"
    )
    forwarded = head + "    def f(self, i):\n        return _map_http_202(self._c.transition(i))\n"
    multi_capture = (
        head + "    def f(self, i):\n        x = _ = self._c.transition(i)\n        return x\n"
    )
    tuple_discard = head + "    def f(self, i):\n        (_,) = self._c.transition(i)\n"
    list_discard = head + "    def f(self, i):\n        [_, _] = self._c.transition(i)\n"
    starred_discard = head + "    def f(self, i):\n        (*_,) = self._c.transition(i)\n"
    mixed_unpack = (
        head + "    def f(self, i):\n        (a, _) = self._c.transition(i)\n        return a\n"
    )

    # an unrelated same-named .transition is not even a coordinator callsite -> ignored entirely
    assert _coordinator_transition_calls(ast.parse(unrelated)) == []
    # a real coordinator bare expression is dropped; so is a `_ =` / `_: X =` discard
    assert dropped_flags(bare) == [True]
    assert dropped_flags(discard) == [True]
    assert dropped_flags(discard_ann) == [True]
    # RECURSIVE all-underscore unpacking (tuple / list / starred) is also a drop
    assert dropped_flags(tuple_discard) == [True]
    assert dropped_flags(list_discard) == [True]
    assert dropped_flags(starred_discard) == [True]
    # each legitimate consumed form is NOT dropped
    assert dropped_flags(assign) == [False]
    assert dropped_flags(returned) == [False]
    assert dropped_flags(matched) == [False]
    assert dropped_flags(forwarded) == [False]
    # a multi-target assign, and an unpack where a REAL name receives part, are consumed
    assert dropped_flags(multi_capture) == [False]
    assert dropped_flags(mixed_unpack) == [False]


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


#: Fixed, deterministic transition timestamps (the patch carries a real updated_at + matching epoch, not
#: now()) so the intended mutation patch + its SHA are reproducible (Yua R13).
_FIXED_UPDATED_AT = "2026-07-13T00:00:00+00:00"
_FIXED_UPDATED_EPOCH = datetime.fromisoformat(_FIXED_UPDATED_AT).timestamp()


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
    # deterministic mutation-patch fields (Yua R13): a fixed updated_at + matching epoch, and an optional
    # supplied lineage key. These are what the patch + patch_sha bind, beyond state+version.
    updated_at: str = _FIXED_UPDATED_AT
    updated_epoch: float = _FIXED_UPDATED_EPOCH
    superseded_by: str | None = None


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


@dataclass(frozen=True)
class _RefRollbackDone:
    """R20 success: the outbox schema was reversed (DROP TABLE lifecycle_outbox committed) and the deploy
    handoff completed while the EX barrier was held through cutover. generation is the durable quiescence
    generation the rollback ran under."""

    generation: int
    kind: str = "rolled_back"


@dataclass(frozen=True)
class _RefCleanupReport:
    """R20 terminal-row cleanup outcome (Yua correction 5): deleted this batch, still-eligible remaining
    (age below cutoff, past the batch), and the total terminal rows."""

    deleted: int = 0
    remaining_eligible: int = 0
    terminal_total: int = 0


class _CleanupConfigError(Exception):
    """R20 config-boundary validation: cutoff must be positive-finite, batch a positive int."""


class _TransientQdrantError(RuntimeError):
    """A KNOWN-transient Qdrant failure - keep the intent PENDING (retryable) (Yua B/J)."""

    terminal = False
    transient = True


class _TerminalQdrantError(RuntimeError):
    """A proven-terminal Qdrant failure - ABANDON, never a FINAL event (Yua J)."""

    terminal = True


class _UnknownQdrantError(RuntimeError):
    """An UNKNOWN/unclassified Qdrant failure (Yua R15): NEVER terminal - retried like a transient and
    kept PENDING, but recorded failure_class='unknown' for later R19 telemetry."""


def _intended_patch(intent: _RefIntent) -> dict[str, object]:
    """The EXACT deterministic mutation patch a transition writes (Yua R13's real patch vocabulary):
    target state, expected_version+1, the fixed updated_at + matching updated_epoch, and any supplied
    lineage key (superseded_by). NOT the whole stored object - unrelated pre-existing payload is outside
    the patch. `missing != null`, so an absent lineage key is simply not in the map."""
    patch: dict[str, object] = {
        "state": intent.target_state,
        "version": intent.expected_version + 1,
        "updated_at": intent.updated_at,
        "updated_epoch": intent.updated_epoch,
    }
    if intent.superseded_by is not None:
        patch["superseded_by"] = intent.superseded_by
    return patch


def _canonical_patch_sha(patch: dict[str, object]) -> str:
    """SHA256 over a collision-safe canonical JSON of EXACTLY the patch map (sorted keys, compact,
    type-preserving). A wrong value/type/timestamp or a missing key changes the digest."""
    return hashlib.sha256(
        json.dumps(patch, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# The frozen, explicit safety default for the in-flight-outbox cap (Yua R14): NO silent unbounded
# production. The future source phase must surface the SAME positive value from Settings; there is no
# None/unbounded option. A test overrides it with a small positive N via the pending_cap kwarg.
DEFAULT_PENDING_CAP = 10_000

# R15 bounded-backoff policy (frozen constants; the future source phase surfaces the same values). A
# transient/unknown retry is paced by next_attempt_epoch = now + min(BASE * 2**(attempts-1), MAX). The
# cap is what makes it BOUNDED; attempts is durable observability, NEVER a retry terminator.
R15_BASE_BACKOFF = 1.0
R15_MAX_BACKOFF = 300.0
# the exponent at which BASE*2**exp already saturates MAX - used to cap the EXPONENT so huge durable
# attempt counts return MAX without ever evaluating an enormous 2**n (overflow-safe).
_R15_SATURATING_EXP = math.ceil(math.log2(R15_MAX_BACKOFF / R15_BASE_BACKOFF))
_R15_FIXED_CLOCK = 1_000_000.0  # only used by the fixed_clock_default WRONG candidate
_R15_WRONG_ABANDON_AT = 5  # attempt count at which the count-based-abandon WRONG candidates give up

# R16/R17 reconciliation-lease policy. A worker atomically CLAIMS a due, unleased-or-expired row with a
# fresh per-claim owner token valid for DEFAULT_LEASE_TTL; only the current owner may commit disposition.
# TTL injection is a CONTRACT SEAM only here - a future source phase must surface a positive-finite value
# from Settings (a SEPARATE source decision; Settings is NOT implied/authorized now).
DEFAULT_LEASE_TTL = 30.0

# R19 observability discipline. The coordinator logs failures through this logger (so pytest caplog / a
# capturing handler sees the SAME records production would) with a stable event code + the bounded
# failure_class ONLY - never operation/object/namespace/content/patch/reason/token. Metrics are a no-label
# pending gauge + a mutation-failures counter whose ONLY label is class in {terminal, transient}.
_LIFECYCLE_LOGGER = logging.getLogger("musubi.lifecycle.coordinator")
# the ONLY keys a persisted outbox patch may contain (Yua R19 minimal canonical target patch).
_CANONICAL_PATCH_KEYS = frozenset(
    {"state", "version", "updated_at", "updated_epoch", "superseded_by"}
)
# the sentinel a test injects into every PII-carrying field; it must NEVER surface in row/log/metrics.
_PII_SENTINEL = "PIISENTINEL"
# the ONLY metric names the coordinator may emit (Yua R19: names whitelisted, never dynamic/PII-bearing).
_ALLOWED_METRIC_NAMES = frozenset(
    {"musubi_lifecycle_outbox_pending", "musubi_lifecycle_outbox_mutation_failures_total"}
)

# R20 rollback/maintenance barrier (Yua 2026-07-13, v3 + 5 corrections). A rollback quiesces the system
# behind a cross-process fcntl.flock barrier on a STABLE per-DB lock file (`<db_path>.maintlock`, fixed
# path, O_CREAT|O_RDWR 0o600, NEVER unlinked/replaced mid-window). Barrier-aware admission/reconcile take
# LOCK_SH for the FULL operation + recheck durable maintenance_active AFTER the lock (closing the
# check-to-lock race); rollback takes LOCK_EX (drains in-flight shared holders, blocks new ones) held
# through BEGIN IMMEDIATE + recheck + count + DROP + deploy-handoff, released ONLY after cutover. The
# lock is OS-released on fd close (crash-safe). No old/new-binary interop (correction 1): a pre-barrier
# process does not take the lock; the deploy sequence stops all pre-barrier processes and starts the old
# target only AFTER schema reversal at cutover.
_MAINTLOCK_SUFFIX = ".maintlock"


class _LogCapture(logging.Handler):
    """Capture LogRecords emitted by the coordinator's logger (the caplog-equivalent for R19)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _validate_lease_ttl(ttl: object) -> float:
    """Config-boundary validation (Yua R16): a positive, FINITE lease TTL. bool is an int subclass, so
    reject it explicitly; reject non-numbers, <= 0, NaN, and inf."""
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise TypeError(f"lease_ttl must be a real number (not bool), got {type(ttl).__name__}")
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError(f"lease_ttl must be positive and finite, got {ttl}")
    return float(ttl)


class _CapExceeded(Exception):
    """Internal sentinel: the atomic admission found the non-terminal backlog at/over the cap (Yua R14).
    transition() maps it to Err(code='cap_exceeded'); no outbox row is written and Qdrant is untouched."""


def _validate_pending_cap(cap: object) -> int:
    """Config-boundary validation (Yua R14 D2/(f)): the cap must be a positive int. bool is a subclass of
    int, so reject it EXPLICITLY; reject any non-int type and any value <= 0. No None/unbounded."""
    if isinstance(cap, bool) or not isinstance(cap, int):
        raise TypeError(f"pending_cap must be an int (not bool), got {type(cap).__name__}")
    if cap <= 0:
        raise ValueError(f"pending_cap must be a positive int, got {cap}")
    return cap


# ---- committed reference/candidate coordinator (test-local, NEVER written to src) ------------------ #


class _RefCoordinator:
    """A reference implementation of the locked Phase-1 API. `mode` selects the correct behavior or a
    named plausible-wrong one so the red-proof discriminates each red. A private `_checkpoint(name)` seam
    (default no-op; NOT a public switch, NO production os._exit) lets tests inject a deterministic fault
    or crash at a named boundary."""

    def __init__(
        self,
        *,
        client: Any = None,
        db_path: Path,
        qdrant_path: Path | None = None,
        mode: str = "correct",
        pending_cap: int = DEFAULT_PENDING_CAP,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> None:
        self._pending_cap = _validate_pending_cap(
            pending_cap
        )  # Yua R14: positive-int, no unbounded
        self._lease_ttl = _validate_lease_ttl(lease_ttl)  # Yua R16: positive-finite
        self._reused_token: str | None = (
            None  # only the reuse_owner_ABA wrong replays a prior token
        )
        self._metrics: list[tuple[str, dict[str, str], float]] = []  # R19: (name, labels, value)
        self._client = client
        # LAZY on-disk Qdrant (R22): a two-process race passes a qdrant_path; Qdrant is opened only when a
        # process actually reads/applies. An active_intent loser (rejected at begin) never opens it; a
        # version_fence loser DOES lazy-open it and issues a zero-match conditional attempt (its readback
        # shows the winner's target), so it never mutates.
        self._qdrant_path = str(qdrant_path) if qdrant_path is not None else None
        self._db = str(db_path)
        self._mode = mode
        # R20 barrier: a STABLE per-DB lock file beside the DB (fixed path, never unlinked mid-window).
        self._maintlock = str(db_path) + _MAINTLOCK_SUFFIX
        # local_flag_only (WRONG): the maintenance state lives in an in-MEMORY flag on this instance
        # instead of the durable lifecycle_control row, so a peer process/instance never observes it.
        self._local_maint = False
        # deploy-handoff seam (R20): a test injects a failing handoff to exercise handoff_failed. Default
        # succeeds. It runs AFTER the DROP+COMMIT while the EX barrier is still held (before cutover).
        self._deploy_handoff: Callable[[], bool] = lambda: True
        self._checkpoint: Any = lambda _name: None
        # R15 injectable clock. PRODUCTION-shaped default is real time.time; a test injects a controllable
        # clock via this seam. fixed_clock_default (WRONG) freezes the DEFAULT to a constant.
        self._now: Callable[[], float] = (
            (lambda: _R15_FIXED_CLOCK) if self._mode == "fixed_clock_default" else time.time
        )
        con = sqlite3.connect(self._db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_outbox (operation_key TEXT PRIMARY KEY, object_id TEXT,"
            " collection TEXT, target_state TEXT, expected_version INTEGER, patch_sha TEXT,"
            " patch_json TEXT,"
            " intent_digest TEXT, state TEXT, event_id TEXT,"
            " attempts INTEGER DEFAULT 0, next_attempt_epoch REAL, failure_class TEXT,"
            " lease_owner TEXT, lease_expires_epoch REAL,"
            # R20 additive: the terminal timestamp (set when a row -> FINAL/ABANDONED); NULL for
            # nonterminal rows and for pre-migration terminal rows whose age is unknown (preserved).
            " terminal_epoch REAL)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_events (event_id TEXT PRIMARY KEY, object_id TEXT,"
            " namespace TEXT, to_state TEXT)"
        )
        # R20 additive single-row control table: the DURABLE quiescence state a rollback sets before it
        # waits for the EX barrier, and every barrier-aware reader rechecks under LOCK_SH.
        con.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_control (id INTEGER PRIMARY KEY CHECK(id=1),"
            " maintenance_active INTEGER DEFAULT 0, generation INTEGER DEFAULT 0)"
        )
        con.execute(
            "INSERT OR IGNORE INTO lifecycle_control (id, maintenance_active, generation) VALUES (1,0,0)"
        )
        # test-local EFFECTIVE-APPLY success markers (Yua R22): one durable row per op/target written at
        # the POST-READBACK-success boundary (not the pre-set_payload attempt), so the parent can assert
        # exactly one effective apply belongs to the winner. non_atomic_cas produces two.
        con.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_apply_markers (operation_key TEXT, object_id TEXT,"
            " target_state TEXT)"
        )
        if self._mode not in ("no_unique_index", "non_atomic_cas"):
            # ATOMIC single-active-intent (Yua R11): a DB-enforced partial unique index, NOT a
            # check-then-insert. Two concurrent begins for one object can't both create a nonterminal row.
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_active_intent ON lifecycle_outbox "
                "(collection, object_id) WHERE state IN ('PENDING','APPLIED')"
            )
        con.commit()
        con.close()

    # -- internals --------------------------------------------------------------------------------- #
    def _qc(self) -> Any:
        """The Qdrant client - lazily opened from qdrant_path if this coordinator was built for a race."""
        if self._client is None and self._qdrant_path is not None:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                self._client = QdrantClient(path=self._qdrant_path)
        return self._client

    # -- R20 rollback/maintenance barrier ---------------------------------------------------------- #
    def _maintenance_active(self) -> bool:
        """The DURABLE maintenance flag every barrier-aware reader rechecks under LOCK_SH. local_flag_only
        (WRONG) reads an in-MEMORY flag instead, so a peer process/instance never observes the quiesce."""
        if self._mode == "local_flag_only":
            return self._local_maint
        con = sqlite3.connect(self._db)
        try:
            row = con.execute(
                "SELECT maintenance_active FROM lifecycle_control WHERE id=1"
            ).fetchone()
        finally:
            con.close()
        return bool(row[0]) if row else False

    def _set_maintenance(self, active: bool, *, bump_generation: bool) -> int:
        """Set the maintenance flag (durably) and optionally bump the quiescence generation. Returns the
        current generation. local_flag_only (WRONG) keeps maintenance_active in memory (generation stays
        durable) so the quiesce is invisible cross-process."""
        con = sqlite3.connect(self._db)
        try:
            if self._mode == "local_flag_only":
                self._local_maint = active  # WRONG: not durable
                if bump_generation:
                    con.execute("UPDATE lifecycle_control SET generation=generation+1 WHERE id=1")
            elif bump_generation:
                con.execute(
                    "UPDATE lifecycle_control SET maintenance_active=?, generation=generation+1 "
                    "WHERE id=1",
                    (int(active),),
                )
            else:
                con.execute(
                    "UPDATE lifecycle_control SET maintenance_active=? WHERE id=1", (int(active),)
                )
            con.commit()
            gen = con.execute("SELECT generation FROM lifecycle_control WHERE id=1").fetchone()[0]
        finally:
            con.close()
        return int(gen)

    @contextmanager
    def _barrier_admit(self, *, role: str) -> Iterator[bool]:
        """Barrier-aware admission (Yua R20): hold LOCK_SH for the FULL operation, then recheck durable
        maintenance AFTER the lock, and REFUSE (yield False) + release if active - so a new reader never
        streams past a quiescing writer and the check-to-lock race is closed. TRANSPARENT when no
        maintenance is active (yields True). role='admission' (transition) | 'reconcile'."""
        mode = self._mode
        if mode == "old_binary_ignores_lock":
            # WRONG: a pre-barrier reader (BOTH roles) that never takes LOCK_SH -> a rollback's LOCK_EX
            # cannot drain it, so it can mutate concurrently with (or after) the schema drop.
            yield True
            return
        if mode == "reconcile_bypasses_barrier" and role == "reconcile":
            # WRONG (role-specific): the RECONCILER skips LOCK_SH + the maintenance recheck while admission
            # still locks correctly, so a rollback cannot drain an in-flight reconcile pass. This is
            # invisible to the admission proof (admission locks fine) and caught ONLY by the reconciler
            # proof - which is why reconcile_once needs its OWN process drain proof, not shared code alone.
            yield True
            return
        # worker_stopped_but_admission_live (WRONG): only reconcilers are quiesced; admission ignores the
        # maintenance flag and keeps admitting through the window.
        skip_recheck = mode == "worker_stopped_but_admission_live" and role == "admission"
        fd = os.open(self._maintlock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if mode == "check_active_before_shared_only":
                # WRONG: recheck maintenance BEFORE taking LOCK_SH, so a rollback that activates between
                # this stale check and the (later) shared lock slips this reader in past the drain.
                active = self._maintenance_active()
                self._checkpoint("active_checked_before_lock")  # test injects activation HERE
                fcntl.flock(fd, fcntl.LOCK_SH)
                yield not active
                return
            fcntl.flock(fd, fcntl.LOCK_SH)  # blocks while a rollback holds LOCK_EX (the drain)
            self._checkpoint(
                "shared_lease_acquired"
            )  # test injects activation HERE (correct rechecks)
            if not skip_recheck and self._maintenance_active() and mode != "starving_new_readers":
                # CORRECT: refuse + release the shared lease.
                yield False
                return
            # starving_new_readers (WRONG): PROCEED holding LOCK_SH even though maintenance is active.
            yield True
            # after the operation completes, while LOCK_SH is STILL held (before the finally releases it):
            # a two-process drain proof uses this to mark that its in-flight critical section has ended.
            self._checkpoint("before_shared_release")
        finally:
            os.close(fd)

    def _key(self, i: _RefIntent) -> str:
        return (
            i.operation_key
            or f"canon:{i.collection}:{i.object_id}:{i.expected_version}:{i.target_state}"
        )

    def _cur(self, collection: str, object_id: str) -> tuple[object, object]:
        return _qdrant_state(self._qc(), collection, object_id)

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

    def _nonterminal_count_sql(self) -> str:
        """The global backlog SELECT for the cap gate (Yua R14). CORRECT counts NON-TERMINAL rows
        (PENDING+APPLIED); terminal FINAL/ABANDONED never count. Two wrong variants mis-scope it."""
        if (
            self._mode == "terminal_counts"
        ):  # WRONG: terminal rows inflate the count -> false early cap
            states = "('PENDING','APPLIED','FINAL','ABANDONED')"
        elif (
            self._mode == "pending_only"
        ):  # WRONG: ignores APPLIED -> undercounts the in-flight backlog
            states = "('PENDING')"
        else:
            states = "('PENDING','APPLIED')"
        return f"SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN {states}"

    def _over_cap(self, con: Any) -> bool:
        count = con.execute(self._nonterminal_count_sql()).fetchone()[0]
        if (
            self._mode == "off_by_one"
        ):  # WRONG: '>' admits one PAST the cap (should reject AT the cap)
            return bool(count > self._pending_cap)
        return bool(count >= self._pending_cap)

    def _backoff(self, attempts: int) -> float:
        """Bounded exponential backoff (Yua R15): min(BASE * 2**(attempts-1), MAX). Overflow-safe - the
        EXPONENT is capped first, so a huge durable attempts count saturates to MAX without ever
        evaluating an enormous 2**n. unbounded_backoff drops the cap; exponent_overflow evaluates 2**n
        directly (float-overflows on huge counts)."""
        if self._mode == "constant_backoff":  # WRONG: a flat delay - not exponential at all
            return R15_BASE_BACKOFF
        exp = max(0, attempts - 1)
        if (
            self._mode == "wrong_exponent_origin"
        ):  # WRONG: 2**attempts (off-by-one) -> backoff(1)=2 not 1
            exp = attempts
        if self._mode == "exponent_overflow_at_huge_attempts":
            return float(R15_BASE_BACKOFF * (2**exp))  # WRONG: 2**9999 -> OverflowError on float()
        if self._mode == "unbounded_backoff":  # WRONG: no MAX cap -> unbounded delay
            capped_exp = min(exp, _R15_SATURATING_EXP + 1)
            return float(R15_BASE_BACKOFF * (2**capped_exp))
        if exp >= _R15_SATURATING_EXP:  # CORRECT: saturate the exponent before computing the power
            return R15_MAX_BACKOFF
        return float(min(R15_BASE_BACKOFF * (2**exp), R15_MAX_BACKOFF))

    def _new_token(self) -> str:
        """A FRESH cryptographically-strong per-CLAIM owner token (Yua R16): the generation/ABA fence.
        NOT derived from operation data, NOT reused. shared_static_owner_token / reuse_owner_ABA are the
        wrongs that break freshness."""
        if self._mode == "shared_static_owner_token":
            return "SHARED-STATIC-TOKEN"  # WRONG: every worker shares one token -> no exclusivity
        if self._mode == "reuse_owner_ABA" and self._reused_token is not None:
            return self._reused_token  # WRONG: replays a prior token -> a stale owner can ABA-match
        tok = secrets.token_hex(16)
        self._reused_token = tok  # remembered ONLY so the reuse_owner_ABA wrong can replay it
        return tok

    def _expiry_clause(self) -> tuple[str, bool]:
        """The WHERE fragment deciding which leases a claim may take (Yua R16/R17 boundary): a valid lease
        (lease_expires_epoch > now) is exclusive; an expired one (<= now) is reclaimable. Returns
        (sql_fragment, needs_now_param)."""
        if self._mode in ("ignore_unexpired_owner", "reclaim_before_expiry"):
            return "", False  # WRONG: claim even a VALID unexpired lease
        if self._mode == "never_reclaim":
            return " AND lease_owner IS NULL", False  # WRONG: never reclaims an EXPIRED lease
        op = (
            "<" if self._mode == "strict_lt_equality_bug" else "<="
        )  # boundary: == now is reclaimable
        return f" AND (lease_owner IS NULL OR lease_expires_epoch {op} ?)", True

    def _claim(self, con: Any, opk: str, now: float, token: str) -> bool:
        """Atomic guarded claim (Yua R16): ONE committed UPDATE over a due, unleased-or-expired row.
        rowcount==1 IS ownership. select_then_update does a NON-atomic check-then-write (a race window)."""
        if self._mode == "ttl_zero":
            expiry = now  # WRONG: the lease expires immediately (== now) -> not exclusive
        elif self._mode == "ttl_unbounded":
            expiry = math.inf  # WRONG: the lease never expires -> a crashed owner is stuck forever
        elif self._mode == "lease_overshoot_ttl":
            expiry = now + self._lease_ttl + 0.5  # WRONG: a lease that outlasts the configured TTL
        else:
            expiry = now + self._lease_ttl
        due_sql = (
            ""
            if self._mode in ("claim_without_due_filter", "backoff_ignored")
            else " AND (next_attempt_epoch IS NULL OR next_attempt_epoch <= ?)"
        )
        exp_sql, exp_needs_now = self._expiry_clause()
        if self._mode == "select_then_update":
            row = con.execute(
                "SELECT lease_owner, lease_expires_epoch FROM lifecycle_outbox WHERE operation_key=?",
                (opk,),
            ).fetchone()
            owner, exp = (row[0], row[1]) if row else (None, None)
            claimable = owner is None or (exp is not None and exp <= now)
            self._checkpoint("after_claim_check_before_write")  # the race window
            if not claimable:
                return False
            con.execute(
                "UPDATE lifecycle_outbox SET lease_owner=?, lease_expires_epoch=? WHERE operation_key=?",
                (token, expiry, opk),
            )
            return True
        params: list[object] = [token, expiry, opk]
        if due_sql:
            params.append(now)
        if exp_needs_now:
            params.append(now)
        cur = con.execute(
            "UPDATE lifecycle_outbox SET lease_owner=?, lease_expires_epoch=? WHERE operation_key=? "
            f"AND state IN ('PENDING','APPLIED'){due_sql}{exp_sql}",
            params,
        )
        return bool(cur.rowcount == 1)

    def _owner_guard(self, owner: str | None, *, drop: bool = False) -> tuple[str, list[object]]:
        """The owner-guard WHERE fragment for a post-claim disposition (Yua R16): only the CURRENT owner
        may write. `drop` (set by the wrong candidates at a specific write site) omits the guard so a
        non-owner write succeeds."""
        if owner is None or drop:
            return "", []
        return " AND lease_owner=?", [owner]

    def _persist_attempt(
        self,
        opk: str,
        *,
        reschedule: bool,
        failure_class: str | None = None,
        state: str | None = None,
        owner: str | None = None,
        release: bool = False,
    ) -> None:
        """R15: increment attempts and (re)schedule next_attempt_epoch (+ optional failure_class / state)
        in ONE SQLite transaction, so a fault mid-persist NEVER advances attempts with a stale/missing
        next_attempt_epoch. R16: when `owner` is given the write is owner-guarded, and `release` clears the
        lease atomically in the SAME transaction. attempts_not_tracked skips the increment;
        non_atomic_attempt_schedule / schedule_written_before_attempts split it across two transactions."""
        now = self._now()
        guard, gp = self._owner_guard(owner, drop=(release and self._mode == "nonowner_release"))
        con = sqlite3.connect(self._db, isolation_level=None)
        try:
            row = con.execute(
                "SELECT attempts FROM lifecycle_outbox WHERE operation_key=?", (opk,)
            ).fetchone()
            base = (row[0] or 0) if row else 0
            attempts = base + (0 if self._mode == "attempts_not_tracked" else 1)
            nxt = (now + self._backoff(attempts)) if reschedule else None
            tail_cols = "next_attempt_epoch=?"
            tail: list[object] = [nxt]
            if failure_class is not None:
                tail_cols += ", failure_class=?"
                tail.append(failure_class)
            if state is not None:
                tail_cols += ", state=?"
                tail.append(state)
                # R20: an ABANDONED disposition is terminal - stamp its age in the same write.
                if state in ("FINAL", "ABANDONED"):
                    tail_cols += ", terminal_epoch=?"
                    tail.append(now)
            if release:
                tail_cols += ", lease_owner=NULL, lease_expires_epoch=NULL"
            if self._mode == "non_atomic_attempt_schedule":
                # WRONG: attempts lands in its OWN committed txn; a fault before the schedule write leaves
                # attempts advanced with a stale next_attempt_epoch (caught by the atomic-under-fault case).
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    f"UPDATE lifecycle_outbox SET attempts=? WHERE operation_key=?{guard}",
                    (attempts, opk, *gp),
                )
                con.execute("COMMIT")
                self._checkpoint("after_attempts_before_schedule")
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    f"UPDATE lifecycle_outbox SET {tail_cols} WHERE operation_key=?{guard}",
                    (*tail, opk, *gp),
                )
                con.execute("COMMIT")
            elif self._mode == "schedule_written_before_attempts":
                # WRONG (mirror): the SCHEDULE lands in its own committed txn first; a fault before the
                # attempts write leaves next_attempt_epoch advanced with a STALE attempts (the other torn
                # order - caught only if the fault proof asserts BOTH fields, not attempts alone).
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    f"UPDATE lifecycle_outbox SET {tail_cols} WHERE operation_key=?{guard}",
                    (*tail, opk, *gp),
                )
                con.execute("COMMIT")
                self._checkpoint("after_attempts_before_schedule")
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    f"UPDATE lifecycle_outbox SET attempts=? WHERE operation_key=?{guard}",
                    (attempts, opk, *gp),
                )
                con.execute("COMMIT")
            else:
                con.execute("BEGIN IMMEDIATE")
                self._checkpoint(
                    "after_attempts_before_schedule"
                )  # a fault here rolls back the WHOLE persist
                con.execute(
                    f"UPDATE lifecycle_outbox SET attempts=?, {tail_cols} WHERE operation_key=?{guard}",
                    (attempts, *tail, opk, *gp),
                )
                con.execute("COMMIT")
        finally:
            con.close()

    def _write_pending(
        self, i: _RefIntent, opk: str, event_id: str, state: str = "PENDING"
    ) -> None:
        """Atomic admission (Yua R14 D3): BEGIN IMMEDIATE -> count NON-TERMINAL globally -> cap gate ->
        INSERT -> COMMIT, all in ONE write transaction so concurrent admissions serialize on the write
        lock. At/over the cap: raise _CapExceeded, write NO row (the caller maps it to Err(cap_exceeded)
        before any Qdrant access). The INSERT is NOT 'OR IGNORE': a partial-unique-index violation (a
        second active intent for the object, R11) RAISES IntegrityError so the loser is rejected."""
        self._checkpoint("before_pending_commit")
        patch = _intended_patch(i)
        if self._mode == "empty_patch":
            patch = {}  # WRONG (R19): persist an EMPTY patch - no required state/version/lineage
        if self._mode == "missing_required_patch":
            patch = {
                k: v for k, v in patch.items() if k != "version"
            }  # WRONG: drop a required field
        if self._mode == "full_payload_storage":
            # WRONG (R19): persist arbitrary payload beyond the minimal canonical patch (leaks
            # namespace/reason/actor - PII - into the stored row).
            patch = {**patch, "namespace": i.namespace, "reason": i.reason, "actor": i.actor}
        if self._mode == "noncanonical_serialization":
            # WRONG (R19): store a NON-canonical serialization + a SHA over that noncanonical form, so the
            # stored patch is neither the canonical string nor matches the canonical SHA (breaks R13).
            patch_json = json.dumps(patch, sort_keys=False, indent=2)
            patch_sha = hashlib.sha256(patch_json.encode()).hexdigest()
        else:
            patch_sha = _canonical_patch_sha(patch)
            patch_json = json.dumps(patch, sort_keys=True, separators=(",", ":"))
        params = (
            opk,
            i.object_id,
            i.collection,
            i.target_state,
            i.expected_version,
            patch_sha,
            patch_json,
            self._intent_digest(i),
            state,
            event_id,
        )
        insert = (
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
            "expected_version,patch_sha,patch_json,intent_digest,state,event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)"
        )
        # cap_after_qdrant/no_cap deliberately DO NOT gate at admission (their defect is exposed later).
        gate = self._mode not in ("no_cap", "cap_after_qdrant")
        con = sqlite3.connect(self._db, isolation_level=None)  # manual transaction control
        try:
            if self._mode == "check_then_insert_race":
                # WRONG (Yua R14 D3): count with NO held write lock, then (after a rendezvous) insert in a
                # SEPARATE step -> two processes both read count<cap and both insert (caught by the race).
                if gate and self._over_cap(con):
                    raise _CapExceeded()
                self._checkpoint("after_cap_count_before_insert")
                con.execute("BEGIN IMMEDIATE")
                con.execute(insert, params)
                con.execute("COMMIT")
            else:
                con.execute("BEGIN IMMEDIATE")
                try:
                    if gate and self._over_cap(con):
                        con.execute("ROLLBACK")
                        raise _CapExceeded()
                    con.execute(insert, params)
                    con.execute("COMMIT")
                except sqlite3.IntegrityError:
                    con.execute("ROLLBACK")
                    raise
        finally:
            con.close()
        self._checkpoint("after_pending_commit")

    def _mark(
        self, opk: str, state: str, *, owner: str | None = None, release: bool = False
    ) -> None:
        """Set state; when `owner` is given the write is owner-guarded (only the current lease owner may
        change state); when `release` is set it clears the lease atomically in the SAME write (Yua R16).
        nonowner_release drops the guard on the releasing write. release_in_separate_txn splits them."""
        sets = "state=?"
        params: list[object] = [state]
        # R20: stamp the terminal timestamp in the SAME write that makes the row terminal, so cleanup can
        # find it by age. Nonterminal writes (e.g. APPLIED) leave terminal_epoch untouched (NULL).
        if state in ("FINAL", "ABANDONED"):
            sets += ", terminal_epoch=?"
            params.append(self._now())
        drop = release and self._mode == "nonowner_release"
        guard, gp = self._owner_guard(owner, drop=drop)
        con = sqlite3.connect(self._db)
        if release and self._mode == "release_in_separate_txn":
            # WRONG: the state change and the lease release are NOT one transaction - a fault between them
            # leaves a terminal row still holding its lease (or a released lease on a non-disposed row).
            con.execute(
                f"UPDATE lifecycle_outbox SET {sets} WHERE operation_key=?{guard}",
                (*params, opk, *gp),
            )
            con.commit()
            self._checkpoint("after_state_before_release")
            con.execute(
                f"UPDATE lifecycle_outbox SET lease_owner=NULL, lease_expires_epoch=NULL "
                f"WHERE operation_key=?{guard}",
                (opk, *gp),
            )
            con.commit()
        else:
            if release:
                sets += ", lease_owner=NULL, lease_expires_epoch=NULL"
            con.execute(
                f"UPDATE lifecycle_outbox SET {sets} WHERE operation_key=?{guard}",
                (*params, opk, *gp),
            )
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

    def _mark_apply_success(self, opk: str, object_id: str, target_state: str) -> None:
        con = sqlite3.connect(self._db)
        con.execute(
            "INSERT INTO lifecycle_apply_markers (operation_key, object_id, target_state) VALUES (?,?,?)",
            (opk, object_id, target_state),
        )
        con.commit()
        con.close()

    def _read_payload(self, collection: str, object_id: str) -> dict[str, object]:
        points, _ = self._qc().scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))
                ]
            ),
            limit=1,
            with_payload=True,
        )
        return dict(points[0].payload or {}) if points else {}

    def _confirm(
        self,
        patch: dict[str, object],
        expected_version: int,
        actual: dict[str, object],
        non_atomic: bool,
    ) -> str:
        """Confirm an apply from the ACTUAL readback. 'fence' = stale/0-match (not applied); 'corrupt' =
        the version landed but the readback patch mismatches (a partial/corrupt apply); 'confirmed' = full
        readback identity/fence + SHA over the intended key set projected from ACTUAL data (Yua R13)."""
        target_state = patch["state"]
        intended_sha = _canonical_patch_sha(patch)
        m = self._mode
        if m == "readback_none":  # WRONG: trusts the attempt, no readback
            return "confirmed"
        if m == "readback_version_only":  # WRONG: version alone
            return "confirmed" if actual.get("version") == expected_version + 1 else "fence"
        if m == "readback_version_state_no_sha":  # WRONG: version+state, no SHA over the patch
            if actual.get("version") != expected_version + 1:
                return "fence"
            return "confirmed" if actual.get("state") == target_state else "corrupt"
        if (
            m == "readback_hash_intended"
        ):  # WRONG: hashes the INTENDED values, not the actual readback
            if actual.get("version") != expected_version + 1:
                return "fence"
            return "confirmed" if _canonical_patch_sha(patch) == intended_sha else "corrupt"
        if (
            non_atomic
        ):  # R12/R22 non-atomic candidates: confirm on state only (fence deliberately dropped)
            return "confirmed" if actual.get("state") == target_state else "corrupt"
        # CORRECT: full readback - fence, every intended key present, identity, and the actual-projected SHA.
        # A wrong VERSION or a wrong STATE means my fenced set_payload matched zero points (I did not
        # apply; the object is at someone else's version/state) -> fence, terminal-abandon-eligible. Only
        # version AND state both correct with a missing/mismatched deeper patch key is a CORRUPT apply
        # (I landed the state but a lineage/timestamp field is wrong) -> recoverable (Yua R13).
        if actual.get("version") != expected_version + 1 or actual.get("state") != target_state:
            return "fence"
        for k in patch:
            if k not in actual:
                return "corrupt"  # an intended patch key did not land (missing != present)
        projected = {k: actual[k] for k in patch}
        return "confirmed" if _canonical_patch_sha(projected) == intended_sha else "corrupt"

    def _apply_conditional(
        self, opk: str, collection: str, object_id: str, patch: dict[str, object]
    ) -> str:
        """Send the EXACT mutation patch (fenced server-side on the expected version), then FULL-readback
        (Yua R13). On a 'confirmed' apply it writes a durable effective-apply marker (Yua R22)."""
        expected_version = int(str(patch["version"])) - 1
        if self._mode == "never_apply":
            # WRONG (Yua R13 hole 1): report a partial WITHOUT ever mutating Qdrant. The object stays at
            # its OLD state+version, yet this yields Ok(Pending) + no marker/event/FINAL + a readback SHA
            # that differs from intended - it satisfies every outcome-shape check. Caught ONLY by asserting
            # the raw payload state+version ACTUALLY landed (i.e. the apply really happened).
            return "corrupt"
        must: list[models.Condition] = [
            models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))
        ]
        # stale_owner_effective_apply (R17 WRONG): a reclaimed/stale owner applies WITHOUT the version
        # fence, so its late attempt is EFFECTIVE (a second mutation) instead of a zero-match no-op. The
        # correct composition relies on R13/R22's version-fenced apply to neutralise a stale attempt.
        non_atomic = self._mode in (
            "warn_only_fence",
            "non_atomic_cas",
            "stale_owner_effective_apply",
        )
        if not non_atomic:
            must.append(
                models.FieldCondition(
                    key="version", match=models.MatchValue(value=expected_version)
                )
            )
        self._qc().set_payload(
            collection_name=collection, payload=dict(patch), points=models.Filter(must=must)
        )
        actual = self._read_payload(collection, object_id)
        status = self._confirm(patch, expected_version, actual, non_atomic)
        if status == "confirmed":
            self._mark_apply_success(opk, object_id, str(patch["state"]))
            if self._mode == "duplicate_apply_marker":
                # WRONG (Yua R13 hole 3): write a SECOND effective-apply marker for the same op -> the
                # healthy control's "exactly one correlated marker" assertion catches it.
                self._mark_apply_success(opk, object_id, str(patch["state"]))
        return status

    def _finalize(
        self,
        opk: str,
        event_id: str,
        object_id: str,
        namespace: str,
        target_state: str,
        owner: str | None = None,
    ) -> None:
        """Atomic FINAL: insert the FINAL lifecycle event AND mark the outbox FINAL in ONE txn. The
        outbox update is GUARDED on the exact APPLIED state (`WHERE state='APPLIED'`, exactly one row) so
        a PENDING row can never jump straight to FINAL (Yua R8 forward guard). R16: when `owner` is given
        the FINAL update is ALSO owner-guarded and CLEARS the lease atomically; a non-owner FINAL matches
        zero rows -> a silent no-op (no FINAL, no event committed). finalize_without_owner_guard /
        stale_owner_finalizes drop the owner guard so a non-owner CAN finalize."""
        if self._mode in ("finalize_dup_event_on_replay", "finalize_rekey_on_replay"):
            # These candidates keep the STABLE event on the FIRST finalize (so R9's first phase passes) and
            # only misbehave on a REPLAYED finalize (an event for this op already exists).
            con = sqlite3.connect(self._db)
            already = con.execute(
                "SELECT 1 FROM lifecycle_events WHERE event_id=?", (event_id,)
            ).fetchone()
            con.close()
            if already:
                con = sqlite3.connect(self._db)
                if self._mode == "finalize_dup_event_on_replay":
                    # WRONG: emit a SECOND event (fresh id), leaving the row's event_id -> two events for
                    # the object (caught by R9's total-event assertion).
                    con.execute(
                        "INSERT INTO lifecycle_events (event_id,object_id,namespace,to_state) "
                        "VALUES (?,?,?,?)",
                        (generate_ksuid(), object_id, namespace, target_state),
                    )
                else:  # finalize_rekey_on_replay
                    # WRONG: re-key the operation's audit - delete the original event, insert a fresh one,
                    # and repoint the outbox row's event_id (caught by R9's stable-event assertions).
                    fresh = generate_ksuid()
                    con.execute("DELETE FROM lifecycle_events WHERE event_id=?", (event_id,))
                    con.execute(
                        "INSERT INTO lifecycle_events (event_id,object_id,namespace,to_state) "
                        "VALUES (?,?,?,?)",
                        (fresh, object_id, namespace, target_state),
                    )
                    con.execute(
                        "UPDATE lifecycle_outbox SET event_id=? WHERE operation_key=?", (fresh, opk)
                    )
                con.execute(
                    "UPDATE lifecycle_outbox SET state='FINAL' WHERE operation_key=? AND state='APPLIED'",
                    (opk,),
                )
                con.commit()
                con.close()
                return
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
        drop = self._mode in ("finalize_without_owner_guard", "stale_owner_finalizes")
        guard, gp = self._owner_guard(owner, drop=drop)
        release = ", lease_owner=NULL, lease_expires_epoch=NULL" if owner is not None else ""
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
                f"UPDATE lifecycle_outbox SET state='FINAL', terminal_epoch=?{release} "
                f"WHERE operation_key=? AND state='APPLIED'{guard}",
                (self._now(), opk, *gp),
            )
            if cur.rowcount != 1:
                # owner path: a non-owner (or non-APPLIED) FINAL matches zero rows -> silent no-op (roll
                # back the event insert too). owner=None path keeps the strict R8 forward guard (raise).
                con.execute("ROLLBACK")
                if owner is None:
                    raise RuntimeError(
                        f"finalize guard: expected exactly one APPLIED row for {opk}, updated {cur.rowcount}"
                    )
                return
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _classify(self, exc: Exception) -> str:
        """3-way failure classification (Yua R15): 'terminal' (proven) -> ABANDON; 'transient' (known) or
        'unknown' (unclassified) -> keep PENDING + retry. An unknown is NEVER terminal - unknown_is_terminal
        is the wrong that abandons it. failure_class is recorded for later R19 telemetry."""
        if self._mode == "classify_all_terminal":
            return "terminal"
        if self._mode == "classify_all_transient":
            return "transient"
        if getattr(exc, "terminal", False):
            return "terminal"
        if getattr(exc, "transient", False):
            return "transient"
        if (
            self._mode == "unknown_is_terminal"
        ):  # WRONG: an unclassified failure abandoned as terminal
            return "terminal"
        return "unknown"

    def _classify_terminal(self, exc: Exception) -> bool:
        return self._classify(exc) == "terminal"

    def _observe_failure(self, *, opk: str, oid: str, ns: str, exc: Exception, cls: str) -> None:
        """R19: a PII-FREE failure log + a bounded-cardinality metric. CORRECT logs ONLY a static event
        code + failure_class (terminal|transient|unknown - low-cardinality, NOT PII), and emits a counter
        whose only label is class in {terminal, transient} (unknown maps to transient). The wrongs leak
        identifiers / the raw exception reason into the log, or high-cardinality labels into the metric."""
        extra: dict[str, object] = {"failure_class": cls}
        if (
            self._mode == "log_interpolation"
        ):  # WRONG: interpolate operation/object/namespace identifiers
            extra["message"] = f"mutation failed op={opk} object={oid} namespace={ns}"
        if (
            self._mode == "raw_exception_reason"
        ):  # WRONG: the raw exception message may carry PII/content
            extra["reason"] = str(exc)
        _LIFECYCLE_LOGGER.warning("lifecycle_mutation_failed", extra={"c6b": extra})
        if self._mode == "omit_metrics":  # WRONG: emit NO metric (no observability at all)
            return
        metric_class = (
            "terminal" if cls == "terminal" else "transient"
        )  # unknown -> transient (bounded)
        labels: dict[str, str] = {"class": metric_class}
        if (
            self._mode == "high_cardinality_unknown_class"
        ):  # WRONG: leaks 'unknown' as a 3rd class label
            labels["class"] = cls
        if (
            self._mode == "object_namespace_labels"
        ):  # WRONG: high-cardinality object/namespace labels
            labels["object_id"] = oid
            labels["namespace"] = ns
        name = "musubi_lifecycle_outbox_mutation_failures_total"
        if self._mode == "dynamic_metric_name":  # WRONG: a PII-bearing DYNAMIC metric name
            name = f"musubi_lifecycle_{opk}_failures"
        self._metrics.append((name, labels, 1.0))
        if self._mode == "double_count_metrics":  # WRONG: emit the counter twice (double-count)
            self._metrics.append((name, labels, 1.0))

    def _observe_pending(self) -> None:
        """R19: the pending-depth gauge - NO labels (a bounded cap-backstop signal)."""
        if self._mode == "omit_metrics":  # WRONG: emit NO gauge sample
            return
        con = sqlite3.connect(self._db)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
            ).fetchone()[0]
        finally:
            con.close()
        self._metrics.append(("musubi_lifecycle_outbox_pending", {}, float(n)))

    def _prometheus_exposition(self) -> str:
        """Render the captured metrics as a Prometheus text exposition (name{k=\"v\",...} value)."""
        lines = []
        for name, labels, value in self._metrics:
            lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")
        return "\n".join(lines)

    # -- public API -------------------------------------------------------------------------------- #
    def transition(self, intent: _RefIntent) -> Any:
        # R20: admission holds LOCK_SH for the FULL operation + rechecks durable maintenance. TRANSPARENT
        # when no maintenance is active (each test owns its DB, so the per-DB lock file never contends).
        with self._barrier_admit(role="admission") as admitted:
            if not admitted:
                return Err(error=_RefError(code="maintenance_active"))
            return self._transition_locked(intent)

    def _transition_locked(self, intent: _RefIntent) -> Any:
        # ignore_operation_key (WRONG): mint a fresh key each call -> a caller retry becomes a second
        # operation instead of the same one (caught by R10).
        opk = generate_ksuid() if self._mode == "ignore_operation_key" else self._key(intent)
        digest = self._intent_digest(intent)
        # cap_before_retry (WRONG, Yua R14 (d)): gate the cap BEFORE resolving operation_key idempotency,
        # so a retry of an EXISTING op at cap is falsely rejected cap_exceeded instead of resolving its row.
        # CORRECT gates the cap only at a NEW admission (inside _write_pending), AFTER idempotency below.
        if self._mode == "cap_before_retry":
            con = sqlite3.connect(self._db, isolation_level=None)
            try:
                over = self._over_cap(con)
            finally:
                con.close()
            if over:
                return Err(error=_RefError(code="cap_exceeded"))
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
            except _CapExceeded:
                # R14: the non-terminal backlog is at/over the cap. No row was written and we have NOT
                # touched Qdrant yet (admission precedes apply) -> bounded Err, Qdrant untouched.
                return Err(error=_RefError(code="cap_exceeded"))
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
            status = self._apply_conditional(
                opk, intent.collection, intent.object_id, _intended_patch(intent)
            )
            if self._mode == "cap_after_qdrant":
                # WRONG (Yua R14): the cap is checked only AFTER mutating Qdrant, so an over-cap intent has
                # ALREADY touched Qdrant by the time it is rejected (caught by the "Qdrant untouched /
                # 0 set_payload" assertion). Count the OTHER non-terminal rows (excluding this just-inserted
                # one) so the admission decision matches begin-time - it merely fires too late.
                con = sqlite3.connect(self._db, isolation_level=None)
                try:
                    others = con.execute(
                        "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED') "
                        "AND operation_key != ?",
                        (opk,),
                    ).fetchone()[0]
                finally:
                    con.close()
                if others >= self._pending_cap:
                    self._mark(opk, "ABANDONED")
                    return Err(error=_RefError(code="cap_exceeded"))
            if status == "fence":
                if self._mode != "mutate_first":
                    self._mark(opk, "ABANDONED")
                return Err(error=_RefError(code="version_fence_violation"))
            if status == "corrupt":
                # R13: the version landed but the readback patch mismatches (partial/corrupt apply). Leave
                # it PENDING/recoverable - NO marker/APPLIED/event/FINAL, no terminal abandon (that
                # disposition is R15/R18/R20, not R13's to invent).
                return Ok(value=_RefPending(operation_key=opk, event_id=event_id))
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

    def _wrong_count_abandon(self, attempts_after: int) -> bool:
        """Count-based termination WRONGs (Yua R15): abandon a transient/unknown once attempts crosses a
        cap. This is precisely the behavior R15 forbids - transient/unknown are NEVER abandoned by count."""
        return (
            self._mode in ("abandon_after_n_attempts", "terminal_on_attempt_cap")
            and attempts_after >= _R15_WRONG_ABANDON_AT
        )

    def reconcile_once(self, *, limit: int = 100) -> _RefReport:
        # R20: the reconciler is a barrier-aware reader too - it holds LOCK_SH for the pass and rechecks
        # durable maintenance, quiescing (empty no-op report) while a rollback is draining/active.
        with self._barrier_admit(role="reconcile") as admitted:
            if not admitted:
                return _RefReport()
            return self._reconcile_locked(limit=limit)

    def _reconcile_locked(self, *, limit: int = 100) -> _RefReport:
        # inside the reconcile critical section, holding LOCK_SH: a two-process drain proof parks here to
        # prove an in-flight RECONCILER is drained by a rollback (a no-op by default; transparent).
        self._checkpoint("reconcile_entered")
        # reconcile_greedy (WRONG): also claims already-FINAL rows and re-affirms the payload -> a second
        # reconcile makes extra set_payload calls (caught by R3's exactly-one/zero-apply instrumentation).
        claim_states = (
            "('PENDING','APPLIED','FINAL')"
            if self._mode == "reconcile_greedy"
            else "('PENDING','APPLIED')"
        )
        now = self._now()
        # backoff_ignored / early_skip (WRONG) claim not-due rows too; CORRECT filters to DUE rows only
        # (next_attempt_epoch NULL or in the past), so a not-due row is never even scanned.
        ignore_due = self._mode in (
            "backoff_ignored",
            "early_skip_increments_attempts",
            "lease_claim_not_due",
            "claim_without_due_filter",
            "report_claimed_as_selected",
            "fixed_first_row_reselection",
        )
        con = sqlite3.connect(self._db)
        select = (
            "SELECT operation_key,object_id,collection,target_state,expected_version,event_id,state,"
            "patch_json,attempts,next_attempt_epoch FROM lifecycle_outbox WHERE state IN "
        )
        # DETERMINISTIC FAIR ordering (Yua R18): oldest-first - never-scheduled (NULL next) first, then
        # earliest next_attempt, then insertion order. A failed poison is rescheduled to the FUTURE, so it
        # sinks to the back and other due rows come first. fixed_first_row_reselection (WRONG) orders by
        # rowid ALONE, ignoring the schedule, so it re-selects the same head-of-line row every pass.
        order_by = (
            " ORDER BY rowid"
            if self._mode == "fixed_first_row_reselection"
            else " ORDER BY (next_attempt_epoch IS NOT NULL), next_attempt_epoch, rowid"
        )
        if ignore_due:
            rows = con.execute(f"{select}{claim_states}{order_by} LIMIT ?", (limit,)).fetchall()
        else:
            rows = con.execute(
                f"{select}{claim_states} AND (next_attempt_epoch IS NULL OR next_attempt_epoch <= ?)"
                f"{order_by} LIMIT ?",
                (now, limit),
            ).fetchall()
        con.close()
        fin = ab = pend = claimed = 0
        for opk, oid, coll, tstate, ver, event_id, state, patch_json, attempts, next_epoch in rows:
            patch: dict[str, object] = (
                json.loads(patch_json) if patch_json else {"state": tstate, "version": ver + 1}
            )
            if self._mode == "scan_increments_attempts":
                self._persist_attempt(
                    opk, reschedule=False
                )  # WRONG: bumps attempts just for scanning
            not_due = next_epoch is not None and next_epoch > now
            if not_due and self._mode == "early_skip_increments_attempts":
                self._persist_attempt(opk, reschedule=False)  # WRONG: increments on a not-due SKIP
                continue
            if not_due and self._mode == "lease_claim_not_due":
                # WRONG: claims/refreshes the lease for a not-due row (without touching Qdrant) - a before-
                # due reconcile must have NO lease side effect either (caught only if the no-op asserts the
                # lease fields, not just attempts/schedule).
                con2 = sqlite3.connect(self._db)
                con2.execute(
                    "UPDATE lifecycle_outbox SET lease_owner=?, lease_expires_epoch=? WHERE operation_key=?",
                    ("reconciler-A", now + 30.0, opk),
                )
                con2.commit()
                con2.close()
                continue
            if not_due and self._mode not in ("backoff_ignored", "claim_without_due_filter"):
                continue  # CORRECT: a not-due row is a true no-op (no attempt, no increment, no lease)
            if (
                state == "FINAL"
            ):  # only reachable under reconcile_greedy (claimed BELOW; FINAL isn't leased)
                self._apply_conditional(
                    opk, coll, oid, patch
                )  # WRONG: re-affirm -> extra apply call
                continue
            # R16: atomically CLAIM the (durable) lease before any Qdrant work; only the current owner
            # processes. rowcount==1 is ownership; a valid unexpired owner (or a lost claim race) -> skip.
            token = self._new_token()
            self._checkpoint("before_claim")  # two-process race barrier (both reach the claim)
            cc = sqlite3.connect(self._db)
            got = self._claim(cc, opk, now, token)
            cc.commit()
            cc.close()
            if not got:
                continue
            claimed += 1
            self._checkpoint(
                "after_claim_before_qdrant"
            )  # durable-claim barrier (R16) / crash point (R17)
            if state == "APPLIED":
                if self._mode == "reconcile_always_apply":
                    self._apply_conditional(opk, coll, oid, patch)  # WRONG: re-apply APPLIED (C3)
                self._finalize(
                    opk, event_id, oid, _NS, tstate, owner=token
                )  # readback-only: no increment
                fin += 1
                continue
            if self._mode == "reconcile_no_apply":
                self._mark(opk, "APPLIED", owner=token)
                self._finalize(
                    opk, event_id, oid, _NS, tstate, owner=token
                )  # WRONG: finalize without applying
                fin += 1
                continue
            # Readback FIRST (C2 recovery): if the mutation is already durably applied (a crash after the
            # Qdrant apply but before the APPLIED commit), recognize it and finalize WITHOUT re-applying
            # (readback-only finalization -> NO attempts increment).
            cur_ver, cur_st = self._cur(coll, oid)
            # reclaim_reapplies_after_readback (R17 WRONG): after a reclaim, RE-APPLY instead of recognising
            # the already-applied mutation via readback -> a SECOND effective apply (caught by R17's crash-
            # after-Qdrant reclaim, which requires exactly one effective mutation).
            if self._mode not in ("reconcile_no_readback", "reclaim_reapplies_after_readback") and (
                cur_ver,
                cur_st,
            ) == (ver + 1, tstate):
                self._mark(opk, "APPLIED", owner=token)
                self._finalize(opk, event_id, oid, _NS, tstate, owner=token)
                fin += 1
                continue
            # ACTUAL Qdrant apply attempt -> this is the ONLY place attempts increments (via _persist_attempt).
            try:
                status = self._apply_conditional(opk, coll, oid, patch)
            except Exception as exc:
                cls = self._classify(exc)
                self._observe_failure(opk=opk, oid=oid, ns=_NS, exc=exc, cls=cls)  # R19: PII-free
                if cls == "terminal":
                    self._persist_attempt(
                        opk,
                        reschedule=False,
                        state="ABANDONED",
                        failure_class="terminal",
                        owner=token,
                        release=True,
                    )
                    ab += 1
                elif self._wrong_count_abandon((attempts or 0) + 1):
                    self._persist_attempt(  # WRONG: count-based abandon of a transient/unknown
                        opk,
                        reschedule=False,
                        state="ABANDONED",
                        failure_class=cls,
                        owner=token,
                        release=True,
                    )
                    ab += 1
                elif self._mode == "false_success_dropping":
                    # WRONG (R18): drop a poison transient row via a FALSE success (mark it FINAL) so it
                    # stops re-appearing - silently losing the intent instead of retrying it forever.
                    self._mark(opk, "FINAL", owner=token, release=True)
                    fin += 1
                else:  # transient OR unknown -> keep PENDING, increment + reschedule (durable, forever)
                    self._persist_attempt(
                        opk, reschedule=True, failure_class=cls, owner=token, release=True
                    )
                    pend += 1
                    if self._mode == "head_of_line_break":
                        break  # WRONG (R18): stop the whole batch at the first transient -> later rows starve
                continue
            if status == "fence":
                self._persist_attempt(
                    opk,
                    reschedule=False,
                    state="ABANDONED",
                    failure_class="terminal",
                    owner=token,
                    release=True,
                )
                ab += 1
                continue
            if (
                status == "corrupt"
            ):  # R13: recoverable -> transient-like retry (durable, never abandoned)
                self._persist_attempt(
                    opk, reschedule=True, failure_class="transient", owner=token, release=True
                )
                pend += 1
                continue
            # confirmed: the attempt happened (increment) + hold the lease through APPLIED, release at FINAL.
            self._checkpoint(
                "after_qdrant_before_applied"
            )  # R17 crash point: Qdrant mutated, row still PENDING+leased
            self._persist_attempt(opk, reschedule=False, owner=token)
            self._mark(opk, "APPLIED", owner=token)
            self._finalize(opk, event_id, oid, _NS, tstate, owner=token)
            fin += 1
        self._observe_pending()  # R19: emit the no-label pending-depth gauge
        reported = len(rows) if self._mode == "report_claimed_as_selected" else claimed
        return _RefReport(claimed=reported, finalized=fin, pending=pend, abandoned=ab)

    # -- R20 rollback / maintenance lifecycle / terminal cleanup ------------------------------------ #
    def _count_nonterminal(self) -> int:
        con = sqlite3.connect(self._db)
        try:
            return int(
                con.execute(
                    "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
                ).fetchone()[0]
            )
        finally:
            con.close()

    def rollback(self, *, expected_generation: int) -> Any:
        """R20 rollback (Yua v3 + 5 corrections): reverse the outbox schema behind the EXCLUSIVE barrier,
        refusing while ANY nonterminal (PENDING/APPLIED/leased) row exists and quiescing admission FIRST.
        Sequence: (1) DURABLY set maintenance_active=1 + bump generation BEFORE waiting for EX
        (correction 2); (2) acquire LOCK_EX (drain in-flight LOCK_SH holders, block new ones);
        (3) BEGIN IMMEDIATE; (4) recheck generation==expected else Err(rollback_refused_stale_generation);
        (5) count PENDING/APPLIED, if>0 Err(rollback_refused_nonterminal) dropping NOTHING (maintenance
        STAYS active - correction 4); (6) else DROP TABLE lifecycle_outbox + COMMIT; (7) deploy-handoff
        with EX STILL held, then release EX at cutover. Handoff failure -> Err(handoff_failed), stays
        quiesced, no auto-release."""
        mode = self._mode
        # check_before_quiesce (WRONG): sample the backlog BEFORE quiescing + before EX; a live admission
        # that lands after this sample but before the drop is invisible and destroyed.
        presample = self._count_nonterminal() if mode == "check_before_quiesce" else None
        # (1) durable quiesce + generation bump BEFORE the drain (correction 2).
        new_gen = self._set_maintenance(True, bump_generation=True)
        # replaced_lock_inode (WRONG): swap the lock inode so the EX below is on a NEW inode and provides
        # NO mutual exclusion against in-flight LOCK_SH holders (and path-probes) on the old inode.
        if mode == "replaced_lock_inode":
            with suppress(FileNotFoundError):
                os.unlink(self._maintlock)
        fd = os.open(self._maintlock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._checkpoint(
                "rollback_pre_lock"
            )  # a drain proof rendezvouses here (imported + quiesced)
            # (2) drain barrier. ack_without_drain / in_flight_old_generation (WRONG) use LOCK_EX|NB and
            # proceed even when they cannot drain, so an in-flight reader is never waited out.
            if mode in ("ack_without_drain", "in_flight_old_generation"):
                with suppress(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX)  # blocks until every in-flight LOCK_SH holder drains
            self._checkpoint("ex_acquired")
            con = sqlite3.connect(self._db, isolation_level=None)
            try:
                # (3) ONE write transaction spanning recheck + count + drop.
                con.execute("BEGIN IMMEDIATE")
                # (4) generation fence: only the rollback whose bump is still current may proceed.
                cur_gen = con.execute(
                    "SELECT generation FROM lifecycle_control WHERE id=1"
                ).fetchone()[0]
                stale = expected_generation != new_gen or cur_gen != new_gen
                if stale and mode != "stale_quiescence_generation":
                    con.execute("COMMIT")
                    return Err(error=_RefError(code="rollback_refused_stale_generation"))
                # (5) nonterminal fence. check_before_quiesce uses its stale presample.
                nonterminal = presample if presample is not None else self._count_nonterminal()
                if mode == "rollback_ignores_nonterminal":
                    nonterminal = 0  # WRONG: ignore live intents and drop anyway (data loss)
                if nonterminal > 0:
                    con.execute(
                        "COMMIT"
                    )  # dropped NOTHING; maintenance STAYS active (correction 4)
                    if mode == "clears_maintenance_on_refuse":
                        # WRONG: a refused rollback must LEAVE maintenance active (only abort_maintenance
                        # clears it); clearing it here lets admission resume behind a stalled rollback.
                        self._set_maintenance(False, bump_generation=False)
                    return Err(error=_RefError(code="rollback_refused_nonterminal"))
                # (6) reverse the schema atomically.
                if mode == "check_then_drop_without_single_txn":
                    # WRONG: the count and the drop are NOT one transaction - closing the count txn opens
                    # a gap with NO write lock, so a racing admission can commit a row that the following
                    # drop then destroys. (CORRECT holds one write lock across the count AND the drop, so a
                    # racing admission is blocked, not lost.)
                    con.execute("COMMIT")
                    self._checkpoint(
                        "rollback_before_drop"
                    )  # the vulnerable gap (no write lock held)
                    con.execute("BEGIN IMMEDIATE")
                else:
                    self._checkpoint(
                        "rollback_before_drop"
                    )  # inside the single BEGIN IMMEDIATE txn
                con.execute("DROP TABLE lifecycle_outbox")
                con.execute("COMMIT")
            finally:
                con.close()
            # (7) deploy-handoff while the EX barrier is STILL held (before cutover).
            if mode == "release_barrier_before_deploy":
                os.close(
                    fd
                )  # WRONG: release EX BEFORE the handoff -> admission can run before cutover
                fd = -1
            if not self._deploy_handoff():
                if mode == "releases_on_handoff_failure":
                    self._set_maintenance(False, bump_generation=False)  # WRONG: resume on failure
                # CORRECT: stay quiesced, hold the barrier, no auto-release.
                return Err(error=_RefError(code="handoff_failed"))
            # success cutover: clear maintenance, then release EX (fd close in finally).
            self._set_maintenance(False, bump_generation=False)
            return Ok(value=_RefRollbackDone(generation=new_gen))
        finally:
            if fd >= 0:
                os.close(fd)

    def abort_maintenance(self, *, expected_generation: int) -> Any:
        """R20 correction 4: the ONLY path that clears a maintenance window left active by a refused
        rollback. Under LOCK_EX, recheck generation==expected, then clear maintenance_active."""
        fd = os.open(self._maintlock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            con = sqlite3.connect(self._db, isolation_level=None)
            try:
                con.execute("BEGIN IMMEDIATE")
                cur_gen = con.execute(
                    "SELECT generation FROM lifecycle_control WHERE id=1"
                ).fetchone()[0]
                if cur_gen != expected_generation:
                    con.execute("COMMIT")
                    return Err(error=_RefError(code="abort_refused_stale_generation"))
                con.execute("UPDATE lifecycle_control SET maintenance_active=0 WHERE id=1")
                con.execute("COMMIT")
            finally:
                con.close()
            return Ok(value=_RefRollbackDone(generation=expected_generation, kind="aborted"))
        finally:
            os.close(fd)

    def backfill_terminal_epoch(self) -> int:
        """R20 migration: stamp terminal_epoch for PRE-EXISTING terminal rows whose age is known (their
        patch's updated_epoch). Rows with no known age keep terminal_epoch NULL and are PRESERVED by
        cleanup (missing age is not a licence to delete)."""
        con = sqlite3.connect(self._db)
        n = 0
        try:
            rows = con.execute(
                "SELECT operation_key, patch_json FROM lifecycle_outbox "
                "WHERE state IN ('FINAL','ABANDONED') AND terminal_epoch IS NULL"
            ).fetchall()
            for opk, pj in rows:
                age: object = None
                with suppress(Exception):
                    age = json.loads(pj).get("updated_epoch") if pj else None
                if isinstance(age, (int, float)) and not isinstance(age, bool):
                    con.execute(
                        "UPDATE lifecycle_outbox SET terminal_epoch=? WHERE operation_key=?",
                        (float(age), opk),
                    )
                    n += 1
            con.commit()
        finally:
            con.close()
        return n

    def cleanup_terminal(self, *, cutoff_epoch: object, batch_limit: object) -> _RefCleanupReport:
        """R20 terminal-row retention (Yua correction 5): ONE atomic named-CTE DELETE of the OLDEST eligible
        terminal rows (FINAL/ABANDONED, non-null age, age < cutoff), ORDER BY terminal_epoch, operation_key
        (deterministic tiebreak), LIMIT batch, with the eligibility predicate REPEATED on the outer DELETE.
        Preserves young terminal rows, NULL-age terminal rows, and EVERY nonterminal (PENDING/APPLIED/
        leased) row. Validates cutoff positive-finite + batch positive-int; idempotent."""
        mode = self._mode
        if (
            isinstance(cutoff_epoch, bool)
            or not isinstance(cutoff_epoch, (int, float))
            or not math.isfinite(cutoff_epoch)
            or cutoff_epoch <= 0
        ):
            raise _CleanupConfigError(f"cutoff_epoch must be positive-finite, got {cutoff_epoch!r}")
        if isinstance(batch_limit, bool) or not isinstance(batch_limit, int) or batch_limit <= 0:
            raise _CleanupConfigError(f"batch_limit must be a positive int, got {batch_limit!r}")

        def _counts(con: Any) -> tuple[int, int]:
            terminal_total = int(
                con.execute(
                    "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED')"
                ).fetchone()[0]
            )
            remaining = int(
                con.execute(
                    "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED') "
                    "AND terminal_epoch IS NOT NULL AND terminal_epoch < ?",
                    (cutoff_epoch,),
                ).fetchone()[0]
            )
            return remaining, terminal_total

        con = sqlite3.connect(self._db, isolation_level=None)
        try:
            if mode == "no_cleanup":  # WRONG: never deletes anything
                remaining, terminal_total = _counts(con)
                return _RefCleanupReport(0, remaining, terminal_total)
            op = ">=" if mode == "cleanup_deletes_young_terminal" else "<"
            order = (
                "ORDER BY terminal_epoch"  # WRONG (nondeterministic_tie): no operation_key tiebreak
                if mode == "nondeterministic_tie"
                else "ORDER BY terminal_epoch, operation_key"
            )
            limit = "" if mode == "cleanup_unbounded_batch" else " LIMIT ?"
            state_in = "" if mode == "delete_nonterminal" else " AND state IN ('FINAL','ABANDONED')"
            if mode == "null_terminal_epoch":
                # WRONG: treat a missing age as ancient (COALESCE(...,0)) instead of PRESERVING it, so
                # NULL-age terminal rows are swept even though their age is unknown.
                notnull = ""
                age_col = "COALESCE(terminal_epoch, 0)"
            else:
                notnull = " AND terminal_epoch IS NOT NULL"
                age_col = "terminal_epoch"
            if mode == "outer_and_inner_predicates_missing":
                # WRONG: the eligibility predicate is missing from BOTH the inner subquery (a bare
                # state-terminal batch selector) AND the outer DELETE, so NULL-age / young rows that sort
                # into the oldest batch are deleted. This proves MISSING ELIGIBILITY, not the repeated-outer
                # -predicate specifically: with an uncorrelated IN-subquery + a unique operation_key PK the
                # OUTER-ONLY form is behaviorally REDUNDANT (correct-vs-outer-dropped delete identical rows,
                # verified), so the exact repeated-outer SQL SHAPE is routed to a future G2/G3 static guard.
                # (The CORRECT SQL below still repeats the predicate on the outer DELETE, per correction 5.)
                inner = "SELECT operation_key FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED')"
                sql = (
                    f"DELETE FROM lifecycle_outbox WHERE operation_key IN ({inner} {order}{limit}) "
                    "RETURNING operation_key"
                )
                params: list[object] = [] if not limit else [batch_limit]
                deleted = len(con.execute(sql, params).fetchall())
                remaining, terminal_total = _counts(con)
                return _RefCleanupReport(deleted, remaining, terminal_total)
            inner_where = f"1=1{state_in}{notnull} AND {age_col} {op} ?"
            inner_sql = (
                f"SELECT operation_key FROM lifecycle_outbox WHERE {inner_where} {order}{limit}"
            )
            inner_params: list[object] = [cutoff_epoch, *([] if not limit else [batch_limit])]
            if mode == "delete_outside_selected_batch":
                # WRONG: the DELETE is re-scoped to EVERY eligible row instead of the atomically-selected
                # batch (the LIMIT/batch bound is lost), so it deletes OUTSIDE the selected batch and can
                # remove far more than it claims. This proves BATCH MISMATCH, not non-atomic
                # select-then-delete: since terminal rows are IMMUTABLE, a two-statement select-then-delete
                # of the selected keys is behaviorally EQUIVALENT to the single-statement CTE here, so exact
                # atomic-SQL-SHAPE enforcement is routed to a future G2/G3 static guard. (CORRECT keeps the
                # single-statement CTE below.)
                con.execute("BEGIN")
                con.execute(inner_sql, inner_params).fetchall()  # the "selected" batch (discarded)
                deleted = len(
                    con.execute(
                        f"DELETE FROM lifecycle_outbox WHERE {inner_where} RETURNING operation_key",
                        [cutoff_epoch],
                    ).fetchall()
                )
                con.execute("COMMIT")
                remaining, terminal_total = _counts(con)
                return _RefCleanupReport(deleted, remaining, terminal_total)
            # CORRECT: one atomic NAMED-CTE DELETE in the exact `WITH sel AS (...) DELETE ... WHERE
            # operation_key IN (SELECT operation_key FROM sel) ...` shape that G2b pins (Yua G2/G3 review) -
            # the eligibility predicate is REPEATED on the outer DELETE. Behaviorally identical to the prior
            # inline `IN (<inner_sql>)` subquery: the CTE `sel` materializes the exact same bounded, ordered
            # batch, and the placeholders bind in the same order (inner cutoff, inner LIMIT, outer cutoff),
            # so a Phase-1 implementation following THIS reference passes both R20 behavior and G2b shape.
            outer_where = f"{state_in}{notnull} AND {age_col} {op} ?"
            sql = (
                f"WITH sel AS ({inner_sql}) DELETE FROM lifecycle_outbox "
                f"WHERE operation_key IN (SELECT operation_key FROM sel)"
                f" AND 1=1{outer_where} RETURNING operation_key"
            )
            params = [*inner_params, cutoff_epoch]
            deleted = len(con.execute(sql, params).fetchall())
            remaining, terminal_total = _counts(con)
            return _RefCleanupReport(deleted, remaining, terminal_total)
        finally:
            con.close()


class _CandidateApi:
    """Mimics the coordinator module namespace for a red-proof candidate."""

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self.TransitionIntent = _RefIntent
        self.TransitionFinal = _RefFinal
        self.TransitionPending = _RefPending

    def LifecycleTransitionCoordinator(
        self,
        *,
        client: Any,
        db_path: Path,
        pending_cap: int = DEFAULT_PENDING_CAP,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> _RefCoordinator:
        return _RefCoordinator(
            client=client,
            db_path=db_path,
            mode=self._mode,
            pending_cap=pending_cap,
            lease_ttl=lease_ttl,
        )


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


def _coordinator(
    client: QdrantClient,
    db_path: Path,
    pending_cap: int = DEFAULT_PENDING_CAP,
    lease_ttl: float = DEFAULT_LEASE_TTL,
) -> Any:
    return _api().LifecycleTransitionCoordinator(
        client=client, db_path=Path(db_path), pending_cap=pending_cap, lease_ttl=lease_ttl
    )


def _intent(
    seed: _Seed,
    *,
    to_state: str,
    operation_key: str | None = None,
    expected_version: int | None = None,
    actor: str = "t",
    reason: str = "r",
    superseded_by: str | None = None,
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
        superseded_by=superseded_by,
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


def _corrupt_apply(
    client: QdrantClient, mutate: Callable[[dict[str, Any], str], None], sup: str
) -> None:
    """PARTIAL/corrupt apply (Yua R13): wrap set_payload so the mutation lands with ONE patch field
    corrupted per `mutate` (state + version otherwise correct), exercising a distinct readback-SHA
    failure mode. `sup` is the intended lineage ksuid, available to faults that need the true value."""
    orig: Any = client.set_payload

    def _partial(*a: Any, **k: Any) -> Any:
        payload = dict(k.get("payload") or {})
        mutate(payload, sup)
        k["payload"] = payload
        return orig(*a, **k)

    client.set_payload = _partial  # type: ignore[method-assign]


def _r13_drop_lineage(p: dict[str, Any], sup: str) -> None:
    p.pop("superseded_by", None)  # (a) key ABSENT from the applied payload


def _r13_null_lineage(p: dict[str, Any], sup: str) -> None:
    p["superseded_by"] = None  # (b) present-NULL where intended non-null (absence != null)


def _r13_type_blur_version(p: dict[str, Any], sup: str) -> None:
    p["version"] = float(
        p["version"]
    )  # (c) int->float: Python == blurs it, canonical JSON does not


def _r13_wrong_updated_at(p: dict[str, Any], sup: str) -> None:
    p["updated_at"] = "1999-01-01T00:00:00+00:00"  # (d) wrong timestamp value


def _r13_mismatched_epoch(p: dict[str, Any], sup: str) -> None:
    p["updated_epoch"] = float(p["updated_epoch"]) + 1.0  # (e) updated_at right, epoch mismatched


def _r13_wrong_lineage(p: dict[str, Any], sup: str) -> None:
    p["superseded_by"] = sup[:-1] + ("A" if sup[-1] != "A" else "B")  # (f) present but WRONG value


# name -> (fault, raw-version type expected AFTER apply, strip_lineage_first). Each mode lands the correct
# state+version but corrupts exactly ONE patch field, so version/state (and, for c/d/e, everything the
# fence checks) still match while the intended-key readback SHA differs. The expected version TYPE lets
# the check assert the int->float blur landed as a float 2.0 (== 2 but a distinct canonical encoding)
# while the rest stay int. strip_lineage_first deletes the object's superseded_by BEFORE the apply so a
# DROPPED write yields a genuinely ABSENT key (the seed carries superseded_by=None and Qdrant set_payload
# MERGES - it cannot delete - so without the strip a dropped write would collapse into present-null).
_R13_CORRUPT_FAULTS: dict[str, tuple[Callable[[dict[str, Any], str], None], type, bool]] = {
    "missing_lineage_key": (_r13_drop_lineage, int, True),
    "present_null_lineage": (_r13_null_lineage, int, False),
    "type_blur_version_int_float": (_r13_type_blur_version, float, False),
    "wrong_updated_at": (_r13_wrong_updated_at, int, False),
    "updated_at_ok_epoch_mismatch": (_r13_mismatched_epoch, int, False),
    "wrong_superseded_by_value": (_r13_wrong_lineage, int, False),
}


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
                "SELECT operation_key,state,event_id,patch_sha,patch_json FROM lifecycle_outbox "
                "WHERE operation_key=?",
                (operation_key,),
            )
        except sqlite3.OperationalError:
            return []
        return [
            {
                "operation_key": r[0],
                "state": r[1],
                "event_id": r[2],
                "patch_sha": r[3],
                "patch_json": r[4],
            }
            for r in cur.fetchall()
        ]
    finally:
        con.close()


def _actual_projected_sha(
    client: QdrantClient, collection: str, object_id: str, patch_json: object
) -> str:
    """Recompute the patch SHA from the ACTUAL Qdrant payload projected onto the stored patch's key set.
    An ABSENT key is OMITTED from the projection (not mapped to None), so a missing key produces a
    DIFFERENT SHA than a present-null value (Yua: distinguish absence explicitly, missing != null)."""
    patch = json.loads(str(patch_json))
    points, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=1,
        with_payload=True,
    )
    actual = dict(points[0].payload or {}) if points else {}
    return _canonical_patch_sha({k: actual[k] for k in patch if k in actual})


def _actual_projected_sha_absence_null(
    client: QdrantClient, collection: str, object_id: str, patch_json: object
) -> str:
    """The OLD, BUGGY projection that collapses an ABSENT key to None (actual.get(k) -> None), so it
    CANNOT tell a missing key from a present-null value - the exact failure the fix prevents. Used only
    to red-proof that distinguishing absence is load-bearing (missing and null must project SAME here)."""
    patch = json.loads(str(patch_json))
    points, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=1,
        with_payload=True,
    )
    actual = dict(points[0].payload or {}) if points else {}
    return _canonical_patch_sha({k: actual.get(k) for k in patch})


def _outbox_for_object(db_path: Path, object_id: str) -> list[dict[str, object]]:
    """Every outbox row for an object (to count operations/active-intents regardless of operation_key)."""
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute(
                "SELECT operation_key,state,event_id,target_state,patch_sha,patch_json "
                "FROM lifecycle_outbox WHERE object_id=?",
                (object_id,),
            )
        except sqlite3.OperationalError:
            return []
        return [
            {
                "operation_key": r[0],
                "state": r[1],
                "event_id": r[2],
                "target_state": r[3],
                "patch_sha": r[4],
                "patch_json": r[5],
            }
            for r in cur.fetchall()
        ]
    finally:
        con.close()


def _event_to_state(db_path: Path, event_id: object) -> object:
    if event_id is None or not Path(db_path).exists():
        return None
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute("SELECT to_state FROM lifecycle_events WHERE event_id=?", (event_id,))
        except sqlite3.OperationalError:
            return None
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _reset_to_pending(db_path: Path, operation_key: str) -> None:
    """TEST-ONLY duplicate/stale-delivery FAULT INJECTION: put a resolved op's outbox row back to PENDING
    so a fresh reconcile re-processes it (R9), preserving the op's stable event_id + expected_version.
    This models a duplicate/replayed delivery of the SAME operation. It is NOT a legal production state
    transition: production code must NEVER move an outbox row FINAL/ABANDONED -> PENDING; terminal states
    are terminal (that is exactly what the idempotency this red asserts relies on)."""
    con = sqlite3.connect(str(db_path))
    con.execute(
        "UPDATE lifecycle_outbox SET state='PENDING' WHERE operation_key=?", (operation_key,)
    )
    con.commit()
    con.close()


def _apply_markers(db_path: Path, object_id: str) -> list[tuple[object, object]]:
    """The durable effective-apply success markers (operation_key, target_state) for an object."""
    if not Path(db_path).exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute(
                "SELECT operation_key,target_state FROM lifecycle_apply_markers WHERE object_id=?",
                (object_id,),
            )
        except sqlite3.OperationalError:
            return []
        return [(r[0], r[1]) for r in cur.fetchall()]
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


def _events_for_object(db_path: Path, object_id: str) -> int:
    if not Path(db_path).exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        try:
            cur = con.execute(
                "SELECT COUNT(*) FROM lifecycle_events WHERE object_id=?", (object_id,)
            )
        except sqlite3.OperationalError:
            return 0
        return int(cur.fetchone()[0])
    finally:
        con.close()


_R14_CAP = 3  # a small test cap; the production default is DEFAULT_PENDING_CAP (10_000)


def _nonterminal_total(db_path: Path) -> int:
    """The GLOBAL non-terminal outbox backlog (PENDING+APPLIED) - exactly what R14's cap gates on."""
    if not Path(db_path).exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
        )
        return int(cur.fetchone()[0])
    finally:
        con.close()


def _fill_outbox(
    db_path: Path,
    collection: str,
    *,
    pending: int = 0,
    applied: int = 0,
    final: int = 0,
    abandoned: int = 0,
    tag: str = "fill",
) -> list[str]:
    """Seed dummy outbox rows in the given states (distinct object_id + operation_key each) to set the
    global backlog for R14 cap tests. Terminal FINAL/ABANDONED rows exist to prove they DON'T count.
    Returns the created operation_keys in insertion order."""
    con = sqlite3.connect(str(db_path))
    opks: list[str] = []
    n = 0
    try:
        for state, count in (
            ("PENDING", pending),
            ("APPLIED", applied),
            ("FINAL", final),
            ("ABANDONED", abandoned),
        ):
            for _ in range(count):
                opk = f"{tag}-op-{n}"
                # pad n into the width remaining after the tag so ids stay DISTINCT for any tag length
                # (a fixed [:27] on a longer tag would truncate the distinguishing digits -> collision).
                oid = f"{tag}{n:0{max(1, 27 - len(tag))}d}"[:27]
                con.execute(
                    "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
                    "expected_version,patch_sha,patch_json,intent_digest,state,event_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        opk,
                        oid,
                        collection,
                        "matured",
                        1,
                        "sha",
                        "{}",
                        f"dig-{n}",
                        state,
                        f"ev-{n}",
                    ),
                )
                opks.append(opk)
                n += 1
        con.commit()
    finally:
        con.close()
    return opks


def _mark_row(db_path: Path, operation_key: str, state: str) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE lifecycle_outbox SET state=? WHERE operation_key=?", (state, operation_key))
    con.commit()
    con.close()


def _outbox_field(db_path: Path, operation_key: str, field: str) -> Any:
    """Read one column of one outbox row (R15: attempts / next_attempt_epoch / failure_class / state)."""
    if not Path(db_path).exists():
        return None
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            f"SELECT {field} FROM lifecycle_outbox WHERE operation_key=?", (operation_key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _set_outbox(db_path: Path, operation_key: str, **fields: object) -> None:
    """Directly set outbox columns (R15 test setup, e.g. seed attempts=9999 / a past next_attempt_epoch)."""
    con = sqlite3.connect(str(db_path))
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(
        f"UPDATE lifecycle_outbox SET {cols} WHERE operation_key=?",
        (*fields.values(), operation_key),
    )
    con.commit()
    con.close()


def _set_checkpoint(coord: Any, hook: Any) -> None:
    coord._checkpoint = hook


# ---- R20 rollback/maintenance/cleanup test helpers ------------------------------------------------- #


def _control_generation(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return int(con.execute("SELECT generation FROM lifecycle_control WHERE id=1").fetchone()[0])
    finally:
        con.close()


def _control_active(db_path: Path) -> bool:
    con = sqlite3.connect(str(db_path))
    try:
        return bool(
            con.execute("SELECT maintenance_active FROM lifecycle_control WHERE id=1").fetchone()[0]
        )
    finally:
        con.close()


def _set_control(
    db_path: Path, *, active: int | None = None, generation: int | None = None
) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        if active is not None:
            con.execute("UPDATE lifecycle_control SET maintenance_active=? WHERE id=1", (active,))
        if generation is not None:
            con.execute("UPDATE lifecycle_control SET generation=? WHERE id=1", (generation,))
        con.commit()
    finally:
        con.close()


def _table_exists(db_path: Path, table: str) -> bool:
    con = sqlite3.connect(str(db_path))
    try:
        return bool(
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
        )
    finally:
        con.close()


def _seed_outbox_row(
    db_path: Path,
    *,
    opk: str,
    oid: str,
    collection: str,
    state: str,
    terminal_epoch: float | None = None,
    updated_epoch: float | None = None,
) -> None:
    """Insert a single outbox row in a chosen state / age directly (R20 cleanup + rollback setup). The
    optional updated_epoch is embedded in patch_json so the migration backfill can source it."""
    patch = {"state": "matured", "version": 2}
    if updated_epoch is not None:
        patch["updated_epoch"] = updated_epoch
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
            "expected_version,patch_sha,patch_json,intent_digest,state,event_id,terminal_epoch) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                opk,
                oid,
                collection,
                "matured",
                1,
                "sha",
                json.dumps(patch, sort_keys=True, separators=(",", ":")),
                f"dig-{opk}",
                state,
                f"ev-{opk}",
                terminal_epoch,
            ),
        )
        con.commit()
    finally:
        con.close()


def _outbox_states(db_path: Path) -> list[str]:
    if not _table_exists(db_path, "lifecycle_outbox"):
        return []
    con = sqlite3.connect(str(db_path))
    try:
        return sorted(
            r[0] for r in con.execute("SELECT operation_key FROM lifecycle_outbox").fetchall()
        )
    finally:
        con.close()


def _probe_barrier_free(maintlock: str) -> bool:
    """A tiny out-of-process probe: open the lock PATH and try LOCK_EX|LOCK_NB. Returns True if the
    exclusive lock was acquired (the barrier is FREE), False if BlockingIOError (a holder is inside). Run
    from a child so it is a genuine cross-process observation (flock is per open-file-description)."""
    src = (
        "import os, fcntl, sys\n"
        f"fd = os.open({maintlock!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    sys.exit(0)\n"  # acquired -> FREE
        "except BlockingIOError:\n"
        "    sys.exit(3)\n"  # blocked -> HELD
    )
    proc = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=30, check=False)
    return proc.returncode == 0


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
    # patch-SHA readback: the stored intended patch SHA must equal the SHA of the ACTUAL Qdrant payload
    # projected onto the patch key set.
    if rows2[0]["patch_sha"] != _actual_projected_sha(
        client, seed.collection, seed.object_id, rows2[0]["patch_json"]
    ):
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


def _check_r13(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Conditional apply + FULL readback patch SHA (Yua R13), table-driven proof completeness. For EACH
    corruption mode the apply lands the correct state+version but corrupts exactly ONE patch field; each
    must be INDEPENDENTLY refused - Ok(Pending), exactly one recoverable PENDING outbox row, NO apply
    marker, NO event, NO FINAL - with the stored intended patch_sha unchanged and the ACTUAL projected
    readback SHA DIFFERING from it (R13 invents no repair policy). A healthy CONTROL then proves an
    unrelated pre-existing payload field OUTSIDE the patch key set does NOT change confirmation. The
    harness (client, seed, db_path) are unused - each case gets a FRESH env so cases cannot cross-
    contaminate; only db_path's parent (the tmp dir) is borrowed as the base."""
    base = Path(db_path).parent
    sup = "supersededby0000000000000000"[:27]  # a real lineage ksuid
    proj_ok: dict[str, str] = {}  # correct projected readback SHA per case
    proj_absence_null: dict[str, str] = {}  # buggy absence-collapsed-to-null projection per case
    for name, (mutate, ver_type, strip_first) in _R13_CORRUPT_FAULTS.items():
        c, s, d = _make_env(base / f"r13-{name}")
        try:
            if strip_first:
                # remove the seed's default superseded_by=None so a dropped write yields an ABSENT key
                # (both the coordinator's readback and the test's projection then see genuine absence).
                c.delete_payload(
                    collection_name=s.collection,
                    keys=["superseded_by"],
                    points=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="object_id", match=models.MatchValue(value=s.object_id)
                            )
                        ]
                    ),
                )
            coord = _coordinator(c, d)
            _corrupt_apply(c, mutate, sup)  # land state+version, corrupt this one field
            intent = _intent(s, to_state="matured", operation_key="op-r13", superseded_by=sup)
            res = coord.transition(intent)
            if not isinstance(res, Ok) or not isinstance(res.value, _api().TransitionPending):
                raise DefectStillPresent(
                    f"corrupt apply [{name}] (state+version right, one patch field wrong) must NOT be "
                    f"confirmed on version/state alone - the full readback SHA must catch it -> Ok(Pending)"
                )
            # hole 1: the apply must have ACTUALLY landed the correct state+version (else a no-op/never-
            # apply candidate would satisfy Pending/no-marker/SHA-diff). Assert the RAW payload directly,
            # with the exact expected type - the int->float blur must be a float 2.0, the rest exact ints.
            ver, st = _qdrant_state(c, s.collection, s.object_id)
            if st != "matured" or ver != s.version + 1 or type(ver) is not ver_type:
                raise DefectStillPresent(
                    f"corrupt apply [{name}] must have LANDED state=matured + version={s.version + 1} "
                    f"({ver_type.__name__}); got state={st!r} version={ver!r} ({type(ver).__name__})"
                )
            rows = _outbox_for_object(d, s.object_id)
            if len(rows) != 1 or rows[0]["state"] != "PENDING":
                raise DefectStillPresent(
                    f"corrupt apply [{name}] must leave exactly one recoverable PENDING row, got "
                    f"{[r['state'] for r in rows]}"
                )
            if _apply_markers(d, s.object_id):
                raise DefectStillPresent(
                    f"corrupt apply [{name}] must write NO effective-apply marker"
                )
            if _events_for_object(d, s.object_id):
                raise DefectStillPresent(
                    f"corrupt apply [{name}] must emit NO audit event / NO FINAL"
                )
            intended_sha = _canonical_patch_sha(_intended_patch(intent))
            if rows[0]["patch_sha"] != intended_sha:
                raise DefectStillPresent(
                    f"corrupt apply [{name}] stored patch_sha must equal the SHA over the intended patch"
                )
            proj_ok[name] = _actual_projected_sha(
                c, s.collection, s.object_id, rows[0]["patch_json"]
            )
            proj_absence_null[name] = _actual_projected_sha_absence_null(
                c, s.collection, s.object_id, rows[0]["patch_json"]
            )
            if proj_ok[name] == intended_sha:
                raise DefectStillPresent(
                    f"corrupt apply [{name}] ACTUAL readback SHA must DIFFER from the intended patch SHA"
                )
        finally:
            c.close()
    # hole 2: missing-key vs present-null must be genuinely DISTINGUISHED, not merely both != intended.
    # The correct projection gives them DIFFERENT SHAs; the buggy absence-collapsed-to-null projection
    # gives them the SAME SHA - proving distinguishing absence is load-bearing (missing != null).
    if proj_ok["missing_lineage_key"] == proj_ok["present_null_lineage"]:
        raise DefectStillPresent(
            "missing-key and present-null must project to DIFFERENT readback SHAs (missing != null)"
        )
    if proj_absence_null["missing_lineage_key"] != proj_absence_null["present_null_lineage"]:
        raise DefectStillPresent(
            "the absence-collapsed-to-null projection must FAIL to distinguish missing from null - if it "
            "does not, distinguishing absence is not what makes missing != null hold"
        )
    # healthy CONTROL: an unrelated pre-existing payload field OUTSIDE the patch key set must NOT change
    # confirmation - the readback SHA covers the intended patch key set ONLY, so extra fields are ignored.
    c, s, d = _make_env(base / "r13-control")
    try:
        c.set_payload(
            collection_name=s.collection,
            payload={"unrelated_marker": "outside-the-patch-keyset"},
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=s.object_id)
                    )
                ]
            ),
        )
        coord = _coordinator(c, d)
        ctrl = _intent(s, to_state="matured", operation_key="op-r13c", superseded_by=sup)
        res = coord.transition(ctrl)
        if not isinstance(res, Ok) or not isinstance(res.value, _api().TransitionFinal):
            raise DefectStillPresent(
                "healthy CONTROL: an unrelated pre-existing payload field must NOT block confirmation "
                "(the readback SHA covers the intended patch key set only) - expected Ok(Final)"
            )
        rows = _outbox_for_object(d, s.object_id)
        if len(rows) != 1 or rows[0]["state"] != "FINAL":
            raise DefectStillPresent(
                f"healthy CONTROL must reach exactly one FINAL row, got {[r['state'] for r in rows]}"
            )
        if _events_for_object(d, s.object_id) != 1:
            raise DefectStillPresent("healthy CONTROL must emit exactly one event")
        # hole 3: EXACTLY one effective-apply marker, correlated to this op + target (not merely truthy) -
        # catches a duplicate-marker candidate.
        if _apply_markers(d, s.object_id) != [("op-r13c", "matured")]:
            raise DefectStillPresent(
                f"healthy CONTROL must write EXACTLY one apply marker == (op-r13c, matured), got "
                f"{_apply_markers(d, s.object_id)}"
            )
        if _qdrant_state(c, s.collection, s.object_id) != (s.version + 1, "matured"):
            raise DefectStillPresent("healthy CONTROL must leave Qdrant at target/v+1")
        if _actual_projected_sha(
            c, s.collection, s.object_id, rows[0]["patch_json"]
        ) != _canonical_patch_sha(_intended_patch(ctrl)):
            raise DefectStillPresent(
                "healthy CONTROL: the ACTUAL projected readback SHA must EQUAL the intended patch SHA"
            )
    finally:
        c.close()


def _check_r14(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Hard in-flight-outbox cap (Yua R14): at the cap on GLOBAL non-terminal (PENDING+APPLIED) rows a
    fresh begin returns Err(cap_exceeded) with NO new row / NO Qdrant / NO event / NO marker; below the
    cap it admits; APPLIED counts and terminal (FINAL/ABANDONED) does not; completing one row frees
    EXACTLY one slot; a same-key retry at cap resolves its row (never falsely cap_exceeded) while a
    changed intent on the same key is a conflict; config is validated. Each scenario uses a FRESH env;
    the harness (client, seed, db_path) supply only the base tmp dir."""
    cap = _R14_CAP
    base = Path(db_path).parent

    def _cap_err(res: Any) -> bool:
        return isinstance(res, Err) and getattr(res.error, "code", None) == "cap_exceeded"

    def _is_final(res: Any) -> bool:
        return isinstance(res, Ok) and isinstance(res.value, _api().TransitionFinal)

    # (b) BELOW the cap (cap-1 non-terminal) a fresh transition is ADMITTED and completes. Build the
    # coordinator FIRST - it owns the schema (and, for the strict-xfail red, _api() raises here).
    c, s, d = _make_env(base / "r14-below")
    try:
        coord = _coordinator(c, d, pending_cap=cap)
        _fill_outbox(d, s.collection, pending=cap - 1)
        res = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-below"))
        if not _is_final(res):
            raise DefectStillPresent(
                "R14 below the cap (cap-1) must ADMIT a fresh transition -> Ok(Final)"
            )
    finally:
        c.close()

    # (b core) AT the cap a fresh transition is REJECTED cap_exceeded: no new row, Qdrant untouched
    # (0 set_payload calls + unchanged version/state), no event, no marker.
    c, s, d = _make_env(base / "r14-atcap")
    try:
        coord = _coordinator(c, d, pending_cap=cap)
        _fill_outbox(d, s.collection, pending=cap)
        before = _qdrant_state(c, s.collection, s.object_id)
        calls = _count_set_payload(c)
        res = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-atcap"))
        if not _cap_err(res):
            raise DefectStillPresent(f"R14 AT the cap must return Err(cap_exceeded), got {res!r}")
        if calls["n"] != 0:
            raise DefectStillPresent(
                "R14 cap rejection must NOT touch Qdrant (0 set_payload calls)"
            )
        if _qdrant_state(c, s.collection, s.object_id) != before:
            raise DefectStillPresent(
                "R14 cap rejection must leave the object's Qdrant state UNCHANGED"
            )
        if _nonterminal_total(d) != cap:
            raise DefectStillPresent(
                f"R14 cap rejection must write NO new row (backlog stays {cap}), got {_nonterminal_total(d)}"
            )
        if _outbox_rows(d, "op-r14-atcap"):
            raise DefectStillPresent(
                "R14 cap rejection must create no outbox row for the rejected op"
            )
        if _events_for_object(d, s.object_id) or _apply_markers(d, s.object_id):
            raise DefectStillPresent("R14 cap rejection must emit no event and no apply marker")
    finally:
        c.close()

    # (a) mixed PENDING+APPLIED reach the cap: an APPLIED row DOES count toward the backlog.
    c, s, d = _make_env(base / "r14-mixed")
    try:
        coord = _coordinator(c, d, pending_cap=cap)
        _fill_outbox(d, s.collection, pending=cap - 1, applied=1)  # cap non-terminal, mixed
        res = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-mixed"))
        if not _cap_err(res):
            raise DefectStillPresent(
                "R14 mixed PENDING+APPLIED must reach the cap (APPLIED must count)"
            )
    finally:
        c.close()

    # (a) terminal rows are EXCLUDED: cap-1 non-terminal + many terminal still ADMITS.
    c, s, d = _make_env(base / "r14-terminal-excluded")
    try:
        coord = _coordinator(c, d, pending_cap=cap)
        _fill_outbox(d, s.collection, pending=cap - 1, final=cap, abandoned=cap)
        res = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-term"))
        if not _is_final(res):
            raise DefectStillPresent(
                "R14 terminal rows must NOT count; cap-1 non-terminal must ADMIT"
            )
    finally:
        c.close()

    # (c) completing/abandoning ONE row frees EXACTLY one slot: at cap -> free one -> exactly one more
    # admission fills it (held PENDING) -> the next fresh object is rejected again.
    c, s, d = _make_env(base / "r14-frees-slot")
    try:
        coord = _coordinator(c, d, pending_cap=cap)
        fillers = _fill_outbox(d, s.collection, pending=cap)
        _mark_row(d, fillers[0], "FINAL")  # free exactly one slot (backlog -> cap-1)
        _fail_set_payload(c, _TransientQdrantError("hold op-r14-slot PENDING"))
        r1 = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-slot"))
        if not (isinstance(r1, Ok) and isinstance(r1.value, _api().TransitionPending)):
            raise DefectStillPresent(
                "R14 freeing one slot must admit exactly one more (held PENDING)"
            )
        _restore_set_payload(c)
        if _nonterminal_total(d) != cap:
            raise DefectStillPresent("R14 the freed slot must be consumed by exactly one admission")
        c2, s2, _ = _make_env(base / "r14-frees-slot-2")  # a different object, same shared db
        try:
            res2 = _coordinator(c2, d, pending_cap=cap).transition(
                _intent(s2, to_state="matured", operation_key="op-r14-slot2")
            )
            if not _cap_err(res2):
                raise DefectStillPresent(
                    "R14 only ONE slot was freed; the next fresh object must reject"
                )
        finally:
            c2.close()
    finally:
        c.close()

    # (d) a same-key RETRY at the cap resolves the existing row (idempotent replay), NOT cap_exceeded;
    # a CHANGED intent on the same key stays a conflict.
    c, s, d = _make_env(base / "r14-retry")
    try:
        coord = _coordinator(c, d, pending_cap=cap)
        _fill_outbox(d, s.collection, pending=cap - 1)
        _fail_set_payload(c, _TransientQdrantError("hold op-r14-retry PENDING"))
        r1 = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-retry"))
        if not (isinstance(r1, Ok) and isinstance(r1.value, _api().TransitionPending)):
            raise DefectStillPresent(
                "R14 (d) setup: op-r14-retry must be admitted+held PENDING at cap-1"
            )
        if _nonterminal_total(d) != cap:  # now AT the cap
            raise DefectStillPresent("R14 (d) setup: backlog must be at the cap before the retry")
        retry = coord.transition(_intent(s, to_state="matured", operation_key="op-r14-retry"))
        if _cap_err(retry):
            raise DefectStillPresent(
                "R14 a same-key retry AT the cap must resolve its existing row, NOT be falsely cap_exceeded"
            )
        if not (isinstance(retry, Ok) and isinstance(retry.value, _api().TransitionPending)):
            raise DefectStillPresent(
                "R14 a same-key retry must replay the in-flight row -> Ok(Pending)"
            )
        changed = coord.transition(
            _intent(
                s, to_state="demoted", operation_key="op-r14-retry"
            )  # same key, different intent
        )
        if _cap_err(changed) or not isinstance(changed, Err):
            raise DefectStillPresent(
                "R14 a changed intent on the same key must be operation_key_conflict, not cap_exceeded"
            )
        if getattr(changed.error, "code", None) != "operation_key_conflict":
            raise DefectStillPresent(
                "R14 changed-intent same-key must return operation_key_conflict"
            )
    finally:
        _restore_set_payload(c)
        c.close()

    # (f) config boundary validation: bool / non-int / <= 0 are rejected by the constructor.
    c, s, d = _make_env(base / "r14-config")
    try:
        for bad_cap in (True, 0, -1, 3.5, "3", None):
            try:
                _coordinator(c, d, pending_cap=bad_cap)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            raise DefectStillPresent(f"R14 config validation must reject pending_cap={bad_cap!r}")
    finally:
        c.close()


_R15_OP = "op-r15"


def _r15_env(base: Path, name: str) -> tuple[QdrantClient, _Seed, Path, Any, dict[str, float]]:
    """A fresh env + coordinator with an INJECTED mutable clock (deterministic backoff). The clock is a
    dict so advancing clock['t'] moves 'now' forward past the backoff so a row becomes due again."""
    c, s, d = _make_env(base / f"r15-{name}")
    coord = _coordinator(c, d)
    clock = {"t": 1_000.0}
    coord._now = lambda: clock["t"]
    return c, s, d, coord, clock


def _r15_hold_pending(coord: Any, c: QdrantClient, s: _Seed, exc: Exception) -> Any:
    """Drive the intent to a durable PENDING by failing the INITIAL synchronous apply (which must NOT
    increment attempts)."""
    _fail_set_payload(c, exc)
    return coord.transition(_intent(s, to_state="matured", operation_key=_R15_OP))


def _check_r15(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Transient/unknown failures NEVER abandoned by attempt count (Yua R15): durable PENDING preserved,
    attempts is observability only (increments once per DUE, CLAIMED, ACTUAL Qdrant attempt), bounded
    overflow-safe backoff, atomic (attempts+schedule) persistence. Termination boundary is EXACTLY
    {success -> FINAL, proven-terminal -> ABANDONED} - never attempt count. Each scenario is a fresh env."""
    base = Path(db_path).parent
    op = _R15_OP

    # (1) N=50 successive transient reconciles -> still PENDING, attempts==50, never ABANDONED, no FINAL;
    #     the INITIAL synchronous apply does NOT increment; backoff stays bounded <= MAX.
    c, s, d, coord, clock = _r15_env(base, "n50")
    try:
        r0 = _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        if not (isinstance(r0, Ok) and isinstance(r0.value, _api().TransitionPending)):
            raise DefectStillPresent("R15 setup: an initial transient apply must hold Ok(Pending)")
        if _outbox_field(d, op, "attempts") != 0:
            raise DefectStillPresent(
                "R15 the INITIAL synchronous apply must NOT increment attempts"
            )
        for _ in range(50):
            clock["t"] += 1_000.0  # advance past any backoff so the row is due
            coord.reconcile_once()
        if _outbox_field(d, op, "state") != "PENDING":
            raise DefectStillPresent(
                f"R15 a transient is NEVER abandoned by count; state={_outbox_field(d, op, 'state')} after 50"
            )
        if _outbox_field(d, op, "attempts") != 50:
            raise DefectStillPresent(
                f"R15 attempts must track exactly 50 due retries, got {_outbox_field(d, op, 'attempts')}"
            )
        if _events_for_object(d, s.object_id):
            raise DefectStillPresent(
                "R15 a never-succeeding transient must emit NO event / NO FINAL"
            )
        nxt = _outbox_field(d, op, "next_attempt_epoch")
        if nxt is None or float(nxt) - clock["t"] > R15_MAX_BACKOFF + 1e-6:
            raise DefectStillPresent(
                f"R15 backoff must be BOUNDED <= {R15_MAX_BACKOFF}, got delay "
                f"{None if nxt is None else float(nxt) - clock['t']}"
            )
    finally:
        c.close()

    # (2) overflow-safe huge count: seed attempts=9999, one transient -> attempts=10000, PENDING, bounded
    #     delay - proving the semantic beyond N=50 with no enormous 2**n.
    c, s, d, coord, clock = _r15_env(base, "huge")
    try:
        _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        _set_outbox(d, op, attempts=9999, next_attempt_epoch=None)
        clock["t"] += 1_000.0
        try:
            coord.reconcile_once()
        except OverflowError as e:
            raise DefectStillPresent(
                "R15 backoff must be overflow-safe at huge attempts (never evaluate 2**huge)"
            ) from e
        if _outbox_field(d, op, "attempts") != 10000 or _outbox_field(d, op, "state") != "PENDING":
            raise DefectStillPresent(
                "R15 attempts=9999 -> transient -> PENDING attempts=10000 (no hidden cap above 50)"
            )
        nxt = _outbox_field(d, op, "next_attempt_epoch")
        if nxt is None or float(nxt) - clock["t"] > R15_MAX_BACKOFF + 1e-6:
            raise DefectStillPresent("R15 huge-count backoff must saturate to <= MAX")
    finally:
        c.close()

    # (3) an UNKNOWN failure is retried like a transient (PENDING, increments) and stays classifiable.
    c, s, d, coord, clock = _r15_env(base, "unknown")
    try:
        _r15_hold_pending(coord, c, s, _UnknownQdrantError("?"))
        clock["t"] += 1_000.0
        coord.reconcile_once()
        if _outbox_field(d, op, "state") != "PENDING":
            raise DefectStillPresent("R15 an UNKNOWN failure must be kept PENDING, never ABANDONED")
        if _outbox_field(d, op, "attempts") != 1:
            raise DefectStillPresent(
                "R15 an unknown reconcile attempt must increment attempts once"
            )
        if _outbox_field(d, op, "failure_class") != "unknown":
            raise DefectStillPresent(
                "R15 an unknown failure must stay classifiable 'unknown' (R19)"
            )
    finally:
        c.close()

    # (4) the AT-DUE transient retry increments EXACTLY once and atomically moves next_attempt_epoch to
    #     now + the EXPECTED formula delay; then a BEFORE-DUE reconcile is a TRUE no-op - no Qdrant attempt
    #     AND no lease side effect: attempts, next_attempt_epoch, lease_owner, lease_expires_epoch all
    #     unchanged (a scan/no-op cannot satisfy the at-due move).
    c, s, d, coord, clock = _r15_env(base, "notdue")
    _fields = ("attempts", "next_attempt_epoch", "lease_owner", "lease_expires_epoch")
    try:
        _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        clock["t"] += 1_000.0
        coord.reconcile_once()  # first at-due retry: attempts 0 -> 1
        if _outbox_field(d, op, "attempts") != 1:
            raise DefectStillPresent(
                "R15 an at-due transient retry must increment attempts EXACTLY once"
            )
        nxt = _outbox_field(d, op, "next_attempt_epoch")
        expected = clock["t"] + coord._backoff(1)
        if nxt is None or abs(float(nxt) - expected) > 1e-6:
            raise DefectStillPresent(
                f"R15 an at-due retry must move next_attempt_epoch to now+delay ({expected}), got {nxt}"
            )
        before = tuple(_outbox_field(d, op, f) for f in _fields)
        q_before = _qdrant_state(c, s.collection, s.object_id)
        calls = _count_set_payload(c)
        coord.reconcile_once()  # NOT due -> true no-op
        if calls["n"] != 0:
            raise DefectStillPresent(
                "R15 a before-due reconcile must NOT attempt Qdrant (0 set_payload)"
            )
        after = tuple(_outbox_field(d, op, f) for f in _fields)
        if after != before:
            raise DefectStillPresent(
                f"R15 before-due reconcile must be a true no-op (incl NO lease side effect): "
                f"{dict(zip(_fields, before))} -> {dict(zip(_fields, after))}"
            )
        if _qdrant_state(c, s.collection, s.object_id) != q_before:
            raise DefectStillPresent("R15 a before-due reconcile must not mutate Qdrant")
    finally:
        c.close()

    # (5) eventual SUCCESS -> FINAL, next_attempt_epoch CLEARED, attempts PRESERVED (incl the success attempt).
    c, s, d, coord, clock = _r15_env(base, "success")
    try:
        _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        for _ in range(3):
            clock["t"] += 1_000.0
            coord.reconcile_once()  # 3 transient -> attempts=3
        _restore_set_payload(c)
        clock["t"] += 1_000.0
        coord.reconcile_once()  # success -> FINAL, attempts=4, next cleared
        if _outbox_field(d, op, "state") != "FINAL":
            raise DefectStillPresent("R15 eventual success must reach FINAL")
        if _outbox_field(d, op, "next_attempt_epoch") is not None:
            raise DefectStillPresent("R15 success must CLEAR next_attempt_epoch")
        if _outbox_field(d, op, "attempts") != 4:
            raise DefectStillPresent(
                "R15 success must PRESERVE attempts (observability, incl success attempt)"
            )
        if _events_for_object(d, s.object_id) != 1:
            raise DefectStillPresent("R15 eventual success must emit exactly one FINAL event")
    finally:
        c.close()

    # (6) PROVEN-TERMINAL -> ABANDONED, next_attempt_epoch CLEARED, attempts PRESERVED (boundary's other side).
    c, s, d, coord, clock = _r15_env(base, "terminal")
    try:
        _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        clock["t"] += 1_000.0
        coord.reconcile_once()  # 1 transient -> attempts=1
        _fail_set_payload(c, _TerminalQdrantError("boom"))
        clock["t"] += 1_000.0
        coord.reconcile_once()  # terminal -> ABANDONED, attempts=2
        if _outbox_field(d, op, "state") != "ABANDONED":
            raise DefectStillPresent("R15 a PROVEN-terminal failure must ABANDON")
        if _outbox_field(d, op, "next_attempt_epoch") is not None:
            raise DefectStillPresent("R15 ABANDONED must clear next_attempt_epoch")
        if _outbox_field(d, op, "attempts") != 2:
            raise DefectStillPresent("R15 ABANDONED must preserve attempts as observability")
        if _events_for_object(d, s.object_id):
            raise DefectStillPresent("R15 an ABANDONED intent must emit NO FINAL event")
    finally:
        c.close()

    # (7) scheduling persistence is ATOMIC under injected failure: a fault mid-persist must leave BOTH
    #     attempts AND next_attempt_epoch unchanged (the whole persist rolls back) - either torn order
    #     (attempts-first OR schedule-first) is caught by asserting the exact (attempts, schedule) tuple.
    c, s, d, coord, clock = _r15_env(base, "atomic")
    try:
        _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        clock["t"] += 1_000.0
        coord.reconcile_once()  # attempts=1, scheduled
        before = (_outbox_field(d, op, "attempts"), _outbox_field(d, op, "next_attempt_epoch"))

        def _fault(name: str) -> None:
            if name == "after_attempts_before_schedule":
                raise sqlite3.OperationalError("injected fault mid-persist")

        coord._checkpoint = _fault
        clock["t"] += 1_000.0
        with suppress(sqlite3.OperationalError):
            coord.reconcile_once()  # the fault propagates; a correct persist rolled BOTH writes back
        after = (_outbox_field(d, op, "attempts"), _outbox_field(d, op, "next_attempt_epoch"))
        if after != before:
            raise DefectStillPresent(
                f"R15 a fault mid-persist must leave BOTH attempts AND next_attempt_epoch unchanged "
                f"(atomic all-or-nothing), got {before} -> {after}"
            )
    finally:
        c.close()

    # (10) EXACT backoff formula (a constant delay or a wrong exponent origin passes a mere <= MAX check).
    #      For BASE=1.0 / MAX=300.0: 1, 2, 4, 8, ... 256, then saturate to 300; huge count -> 300; and
    #      overflow-safe at attempts=10000. A pure boundary check - no wall iterations.
    c, s, d = _make_env(base / "r15-backoff")
    try:
        coord = _coordinator(c, d)
        expected = {1: 1.0, 2: 2.0, 3: 4.0, 4: 8.0, 9: 256.0, 10: 300.0, 11: 300.0, 10000: 300.0}
        for att, want in expected.items():
            got = coord._backoff(att)
            if abs(float(got) - want) > 1e-6:
                raise DefectStillPresent(
                    f"R15 backoff(attempts={att}) must equal {want} (BASE*2**(attempts-1) capped at MAX), got {got}"
                )
    finally:
        c.close()

    # (8) readback-only finalization (C2 recovery) must NOT increment attempts.
    c, s, d, coord, clock = _r15_env(base, "readback")
    try:
        _r15_hold_pending(coord, c, s, _TransientQdrantError("t"))
        _restore_set_payload(c)
        c.set_payload(  # simulate the mutation already durably applied (crash-after-apply)
            collection_name=s.collection,
            payload=_intended_patch(_intent(s, to_state="matured", operation_key=op)),
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=s.object_id)
                    )
                ]
            ),
        )
        clock["t"] += 1_000.0
        coord.reconcile_once()  # readback-only finalize
        if _outbox_field(d, op, "attempts") != 0:
            raise DefectStillPresent("R15 readback-only finalization must NOT increment attempts")
        if _outbox_field(d, op, "state") != "FINAL":
            raise DefectStillPresent("R15 readback-only recovery must reach FINAL")
    finally:
        c.close()

    # (9) the DEFAULT clock is production-shaped real time.time (never a frozen constant).
    c, s, d = _make_env(base / "r15-clock")
    try:
        coord = _coordinator(c, d)  # do NOT inject a clock
        if abs(float(coord._now()) - time.time()) > 5.0:
            raise DefectStillPresent(
                "R15 the coordinator's DEFAULT clock must be real time.time (production-shaped)"
            )
    finally:
        c.close()


def _r16_pending(coord: Any, c: QdrantClient, s: _Seed, opk: str) -> None:
    """Create a durable PENDING outbox row for the REAL seed object (its initial apply held transient)."""
    _fail_set_payload(c, _TransientQdrantError("t"))
    coord.transition(_intent(s, to_state="matured", operation_key=opk))
    _restore_set_payload(c)


def _seed_extra(client: QdrantClient, content: str) -> _Seed:
    """Create an additional real episodic object (R18 needs several distinct due rows)."""
    plane = EpisodicPlane(client=client, embedder=FakeEmbedder())
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=_NS, content=content)))
    return _Seed(
        collection=str(collection_for_plane("episodic")),
        object_id=str(obj.object_id),
        namespace=_NS,
        version=int(obj.version),
    )


def _fail_set_payload_for(client: QdrantClient, object_id: str, exc: Exception) -> None:
    """Wrap set_payload to raise `exc` ONLY for the given object_id (a selective 'poison' row - every
    other object applies normally). The object_id is read from the points Filter's object_id condition."""
    orig: Any = client.set_payload

    def _f(*a: Any, **k: Any) -> Any:
        pts = k.get("points")
        oid = None
        for cond in getattr(pts, "must", None) or []:
            if getattr(cond, "key", None) == "object_id":
                oid = getattr(getattr(cond, "match", None), "value", None)
        if oid == object_id:
            raise exc
        return orig(*a, **k)

    client.set_payload = _f  # type: ignore[method-assign]


def _r17_probe_claimable(coord: Any, d: Path, opk: str, now: float) -> bool:
    """Probe whether a row is claimable at `now` WITHOUT persisting (roll the claim back)."""
    coord._now = lambda: now
    con = sqlite3.connect(str(d))
    try:
        got = coord._claim(con, opk, now, coord._new_token())
        con.rollback()
        return bool(got)
    finally:
        con.close()


def _check_r17(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Expired-owner reclaim safety (Yua R17): the expiry boundary is exact (expires>now valid,
    <=now reclaimable); a reclaim mints a FRESH owner token so a stale owner's OLD token can never
    ABA-match; and a stale/expired owner can neither finalize a row the current owner holds nor apply
    effectively (the R13/R22 version fence makes its late attempt a zero-match no-op). Each scenario is a
    fresh env with an injected clock. The reclaim CRASH matrix (real process death) is _check_r17_reclaim."""
    base = Path(db_path).parent
    E = 5_000.0  # a lease's expiry epoch

    # (1) EXACT expiry boundary: unexpired (expires>now) NOT reclaimable; == and > now (expired) ARE.
    c, s, d = _make_env(base / "r17-boundary")
    try:
        coord = _coordinator(c, d)
        _r16_pending(coord, c, s, "op-b")
        _set_outbox(d, "op-b", lease_owner="old-owner", lease_expires_epoch=E)
        if _r17_probe_claimable(coord, d, "op-b", E - 1.0):
            raise DefectStillPresent("R17 an UNEXPIRED lease (expires>now) must NOT be reclaimable")
        if _r17_probe_claimable(coord, d, "op-b", E - 0.001):  # clock-skew bound: still unexpired
            raise DefectStillPresent(
                "R17 a lease 1ms before expiry must NOT be reclaimable (skew bound)"
            )
        if not _r17_probe_claimable(coord, d, "op-b", E):  # equality: reclaimable AT expiry
            raise DefectStillPresent("R17 at expiry (expires==now) the lease MUST be reclaimable")
        if not _r17_probe_claimable(coord, d, "op-b", E + 1.0):  # expired: reclaimable
            raise DefectStillPresent("R17 an EXPIRED lease (expires<now) MUST be reclaimable")
    finally:
        c.close()

    # (2) ABA: after the lease expires, a reclaim mints a FRESH token; a stale owner's OLD token must NOT
    #     match the reclaimed row (owner guard). reuse_owner_ABA replays the old token -> it matches.
    c, s, d = _make_env(base / "r17-aba")
    try:
        coord = _coordinator(c, d)
        _r16_pending(coord, c, s, "op-aba")
        token_a = coord._new_token()  # A's original claim token
        _set_outbox(d, "op-aba", lease_owner=token_a, lease_expires_epoch=E)
        now_b = E + 1.0
        coord._now = lambda: now_b
        token_b = (
            coord._new_token()
        )  # B's reclaim token (correct: fresh; reuse_owner_ABA: == token_a)
        cc = sqlite3.connect(str(d))
        if not coord._claim(cc, "op-aba", now_b, token_b):
            raise DefectStillPresent("R17 B must be able to reclaim an EXPIRED lease")
        cc.commit()
        cc.close()
        coord._mark(
            "op-aba", "PENDING", owner=token_a, release=True
        )  # stale A's OLD-token disposition
        if _outbox_field(d, "op-aba", "lease_owner") != token_b:
            raise DefectStillPresent(
                "R17 ABA: a stale owner's OLD token must NOT match the reclaimer's fresh lease (owner guard)"
            )
    finally:
        c.close()

    # (3) a STALE/expired owner must NOT finalize a row the CURRENT owner (B) holds - only the current owner
    #     commits SQLite disposition. Correct: A's finalize is owner-guarded -> no-op -> row stays APPLIED.
    c, s, d = _make_env(base / "r17-stale-finalize")
    try:
        coord = _coordinator(c, d)
        _r16_pending(coord, c, s, "op-sf")
        token_b = coord._new_token()  # B currently owns an APPLIED row (mid-processing)
        _set_outbox(d, "op-sf", state="APPLIED", lease_owner=token_b, lease_expires_epoch=E + 100.0)
        ev = _outbox_field(d, "op-sf", "event_id")
        coord._finalize("op-sf", ev, s.object_id, s.namespace, "matured", owner=coord._new_token())
        if _outbox_field(d, "op-sf", "state") == "FINAL":
            raise DefectStillPresent(
                "R17 a stale/non-current owner must NOT finalize a row the current owner holds"
            )
    finally:
        c.close()

    # (4) a STALE owner's late apply must be a ZERO-MATCH no-op (the R13/R22 version fence), never an
    #     effective overwrite of a newer state. B reclaims+applies (v->v+1 matured); a THIRD op moves the
    #     object v+1->v+2 demoted; then stale A (expected_version=v) resumes - its fenced apply must not
    #     overwrite. stale_owner_effective_apply drops the fence and clobbers the newer state.
    c, s, d = _make_env(base / "r17-stale-apply")
    try:
        coord = _coordinator(c, d)
        v = s.version
        _r16_pending(coord, c, s, "op-A")  # A's intent: expected_version=v, target matured
        _set_outbox(d, "op-A", lease_owner=coord._new_token(), lease_expires_epoch=E)
        coord._now = lambda: E + 1.0
        coord.reconcile_once()  # B reclaims op-A, applies (v->v+1 matured), FINAL
        if _qdrant_state(c, s.collection, s.object_id) != (v + 1, "matured"):
            raise DefectStillPresent("R17 setup: B's reclaim must land the object at matured/v+1")
        third = coord.transition(
            _intent(s, to_state="demoted", operation_key="op-C", expected_version=v + 1)
        )
        if not (isinstance(third, Ok) and isinstance(third.value, _api().TransitionFinal)):
            raise DefectStillPresent("R17 setup: the third op must move the object to demoted/v+2")
        if _qdrant_state(c, s.collection, s.object_id) != (v + 2, "demoted"):
            raise DefectStillPresent("R17 setup: object must be at demoted/v+2 before A resumes")
        # stale A resumes its IN-FLIGHT apply (expected_version=v, target matured/v+1).
        a_patch = _intended_patch(
            _intent(s, to_state="matured", operation_key="op-A", expected_version=v)
        )
        coord._apply_conditional("op-A", s.collection, s.object_id, a_patch)
        if _qdrant_state(c, s.collection, s.object_id) != (v + 2, "demoted"):
            raise DefectStillPresent(
                "R17 a stale owner's late apply must be a ZERO-MATCH no-op (fenced), never overwrite the "
                "newer state - exactly one effective mutation survives"
            )
    finally:
        c.close()


def _check_r16(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Valid-lease exclusivity (Yua R16): a worker atomically CLAIMS a due, unleased-or-expired row (one
    guarded UPDATE, rowcount==1 IS ownership); an unexpired owner is exclusive; every post-claim
    disposition is owner-guarded and clears the lease atomically; `claimed` counts successful claims, not
    scanned rows; lease_ttl is positive-finite. Each scenario is a fresh env with an injected clock."""
    base = Path(db_path).parent
    T = 5_000.0

    # (1) `claimed` on a MIXED batch: one claimable due row (A), one validly-leased row (B), one not-due
    #     row (C). CORRECT claims only A -> claimed==1, finalized==1; a scanned count would report 3.
    c, s, d = _make_env(base / "r16-claimed")
    try:
        coord = _coordinator(c, d)
        coord._now = lambda: T
        _r16_pending(coord, c, s, "op-A")  # A: PENDING, due, unleased -> claim + apply -> FINAL
        others = _fill_outbox(d, s.collection, pending=2, tag="mb")
        _set_outbox(
            d, others[0], lease_owner="other-worker", lease_expires_epoch=T + 1_000.0
        )  # B valid lease
        _set_outbox(d, others[1], next_attempt_epoch=T + 1_000.0)  # C not-due
        rep = coord.reconcile_once()
        if rep.claimed != 1:  # report_claimed_as_selected fails HERE (the claimed element only)
            raise DefectStillPresent(
                f"R16 `claimed` must count SUCCESSFUL guarded claims (1), not scanned rows; got {rep.claimed}"
            )
        if (rep.finalized, rep.pending, rep.abandoned, rep.failed) != (1, 0, 0, 0):
            raise DefectStillPresent(
                f"R16 mixed batch report must be exactly finalized=1/pending=0/abandoned=0/failed=0; got "
                f"{(rep.finalized, rep.pending, rep.abandoned, rep.failed)}"
            )
        if _outbox_field(d, others[0], "lease_owner") != "other-worker":
            raise DefectStillPresent(
                "R16 a validly-leased row (B) must be left untouched by another worker"
            )
    finally:
        c.close()

    # (2) a CLAIMED lease is EXCLUSIVE (expires > now) AND BOUNDED (<= now+ttl); a second owner cannot
    #     re-claim a validly-leased row.
    c, s, d = _make_env(base / "r16-exclusive")
    try:
        coord = _coordinator(c, d, lease_ttl=30.0)
        coord._now = lambda: T
        _r16_pending(coord, c, s, "op-x")
        token = coord._new_token()
        cc = sqlite3.connect(str(d))
        got = coord._claim(cc, "op-x", T, token)
        cc.commit()
        cc.close()
        if not got:
            raise DefectStillPresent("R16 a due, unleased row must be claimable")
        exp = _outbox_field(d, "op-x", "lease_expires_epoch")
        if exp is None or not (T < float(exp) <= T + 30.0 + 1e-6):
            raise DefectStillPresent(
                f"R16 a claimed lease must be EXCLUSIVE (expires>now) AND BOUNDED (<=now+ttl); got {exp} at {T}"
            )
        if _outbox_field(d, "op-x", "lease_owner") != token:
            raise DefectStillPresent("R16 the claim must record the owner token")
        cc = sqlite3.connect(str(d))
        got2 = coord._claim(cc, "op-x", T, coord._new_token())
        cc.commit()
        cc.close()
        if got2:
            raise DefectStillPresent(
                "R16 a validly-leased row must NOT be re-claimable by another owner"
            )
    finally:
        c.close()

    # (3) a NON-owner cannot FINALIZE a leased row (owner guard); a shared/static token collides and gets
    #     through, a dropped guard gets through.
    c, s, d = _make_env(base / "r16-nonowner-finalize")
    try:
        coord = _coordinator(c, d)
        coord._now = lambda: T
        _r16_pending(coord, c, s, "op-nf")
        owner_a = coord._new_token()
        _set_outbox(d, "op-nf", state="APPLIED", lease_owner=owner_a, lease_expires_epoch=T + 30.0)
        ev = _outbox_field(d, "op-nf", "event_id")
        coord._finalize("op-nf", ev, s.object_id, s.namespace, "matured", owner=coord._new_token())
        if _outbox_field(d, "op-nf", "state") == "FINAL":
            raise DefectStillPresent("R16 a NON-owner must NOT be able to finalize a leased row")
    finally:
        c.close()

    # (4) a NON-owner cannot RELEASE another owner's lease.
    c, s, d = _make_env(base / "r16-nonowner-release")
    try:
        coord = _coordinator(c, d)
        coord._now = lambda: T
        _r16_pending(coord, c, s, "op-nr")
        owner_a = coord._new_token()
        _set_outbox(d, "op-nr", lease_owner=owner_a, lease_expires_epoch=T + 30.0)
        coord._mark("op-nr", "PENDING", owner=coord._new_token(), release=True)
        if _outbox_field(d, "op-nr", "lease_owner") != owner_a:
            raise DefectStillPresent("R16 a NON-owner must NOT release/clear another owner's lease")
    finally:
        c.close()

    # (5) disposition state change + lease release are ONE owner-guarded transaction: a fault between them
    #     must not leave a terminal row still holding its lease.
    c, s, d = _make_env(base / "r16-release-atomic")
    try:
        coord = _coordinator(c, d)
        coord._now = lambda: T
        _r16_pending(coord, c, s, "op-ra")
        owner_a = coord._new_token()
        _set_outbox(d, "op-ra", lease_owner=owner_a, lease_expires_epoch=T + 30.0)

        def _fault(name: str) -> None:
            if name == "after_state_before_release":
                raise sqlite3.OperationalError("fault between state and release")

        coord._checkpoint = _fault
        with suppress(sqlite3.OperationalError):
            coord._mark("op-ra", "ABANDONED", owner=owner_a, release=True)
        if (
            _outbox_field(d, "op-ra", "state") == "ABANDONED"
            and _outbox_field(d, "op-ra", "lease_owner") is not None
        ):
            raise DefectStillPresent(
                "R16 a fault mid-disposition must not leave a terminal row still holding its lease "
                "(state + release must be one transaction)"
            )
    finally:
        c.close()

    # (6) config boundary: lease_ttl must be positive and FINITE.
    c, s, d = _make_env(base / "r16-ttl-config")
    try:
        for bad in (True, 0, -1, float("inf"), float("nan"), "x", None):
            try:
                _coordinator(c, d, lease_ttl=bad)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            raise DefectStillPresent(f"R16 lease_ttl validation must reject {bad!r}")
    finally:
        c.close()

    # (7) termination / lease-clearing matrix: every HANDLED disposition clears BOTH lease columns in the
    #     SAME owner-guarded state/attempt/schedule transaction; a CRASH preserves the reclaimable lease.
    def _tup(dd: Path, k: str) -> tuple[Any, ...]:
        return tuple(
            _outbox_field(dd, k, f)
            for f in (
                "state",
                "attempts",
                "next_attempt_epoch",
                "failure_class",
                "lease_owner",
                "lease_expires_epoch",
            )
        )

    cases = (
        # (name, inject-fail-exc-or-None, expected full tuple after reconcile)
        ("success", None, ("FINAL", 1, None, None, None, None)),
        ("transient", _TransientQdrantError("t"), ("PENDING", 1, T + 1.0, "transient", None, None)),
        ("unknown", _UnknownQdrantError("?"), ("PENDING", 1, T + 1.0, "unknown", None, None)),
        ("terminal", _TerminalQdrantError("x"), ("ABANDONED", 1, None, "terminal", None, None)),
    )
    for name, exc, want in cases:
        c, s, d = _make_env(base / f"r16-term-{name}")
        try:
            coord = _coordinator(c, d)
            coord._now = lambda: T
            _r16_pending(coord, c, s, f"op-{name}")
            if exc is not None:
                _fail_set_payload(c, exc)
            coord.reconcile_once()
            got = _tup(d, f"op-{name}")
            if got != want:
                raise DefectStillPresent(f"R16 {name} disposition tuple must be {want}, got {got}")
        finally:
            _restore_set_payload(c)
            c.close()

    # crash after claim / pre-Qdrant: state/attempts/schedule unchanged, owner + FINITE expiry retained.
    c, s, d = _make_env(base / "r16-term-crash")
    try:
        coord = _coordinator(c, d, lease_ttl=30.0)
        coord._now = lambda: T
        _r16_pending(coord, c, s, "op-crash")

        def _crash(name: str) -> None:
            if name == "after_claim_before_qdrant":
                raise RuntimeError("simulated crash after claim, before Qdrant")

        coord._checkpoint = _crash
        with suppress(RuntimeError):
            coord.reconcile_once()
        st, att, nxt, fc, owner, exp = _tup(d, "op-crash")
        if (st, att, nxt, fc) != ("PENDING", 0, None, None):
            raise DefectStillPresent(
                f"R16 crash-after-claim must leave state/attempts/schedule unchanged, got {(st, att, nxt, fc)}"
            )
        if (
            owner is None
            or exp is None
            or not math.isfinite(float(exp))
            or not (T < float(exp) <= T + 30.0 + 1e-6)
        ):
            raise DefectStillPresent(
                f"R16 crash-after-claim must RETAIN the reclaimable lease (owner + finite expiry in the "
                f"TTL window), got owner={owner} expiry={exp}"
            )
    finally:
        c.close()


_R16_RACE_WIN = (
    61  # this process won the guarded claim (exits at the post-claim/pre-Qdrant barrier)
)
_R16_RACE_LOSE = 62  # this process lost the claim (rowcount 0) and processed nothing
# a DETERMINISTIC injected claim clock for the race children, so the winner's lease expiry is EXACTLY
# _R16_RACE_CLOCK + DEFAULT_LEASE_TTL - the parent asserts that exact bound with no real-time slack, which
# reliably red-proofs a lease that outlasts the configured TTL (lease_overshoot_ttl).
_R16_RACE_CLOCK = 5_000_000.0


def _r16_race_child_source(*, mode: str, db_path: Path, opk: str, barrier_dir: Path) -> str:
    """A child that races the atomic claim on ONE due, unleased row. It exits WIN if it claims (durably,
    before any Qdrant work) or LOSE if the claim's rowcount is 0. Two WINs means the claim was not atomic."""

    def one(fn: str, tag: str) -> str:
        return (
            f"def {fn}():\n"
            f"    open(_os.path.join(_bd, {tag!r} + '.' + str(_os.getpid())), 'w').close()\n"
            f"    for _ in range(int({_RACE_BARRIER_TIMEOUT!r} / 0.02)):\n"
            f"        if len([f for f in _os.listdir(_bd) if f.startswith({tag!r})]) >= 2: return\n"
            "        _time.sleep(0.02)\n"
            "    raise SystemExit('barrier timeout')\n"
        )

    return (
        "import os, warnings\n"
        "import os as _os, time as _time\n"
        f"_bd = {str(barrier_dir)!r}\n"
        "from qdrant_client import QdrantClient\n"
        "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord\n"
        + one("_await_before_claim", "beforeclaim")
        + one("_await_claim2", "claim2")
        + "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    _client = QdrantClient(':memory:')\n"
        "_calls = {'n': 0}\n"
        "_orig_sp = _client.set_payload\n"
        "def _sp(*a, **k):\n"
        "    _calls['n'] += 1\n"
        "    return _orig_sp(*a, **k)\n"
        "_client.set_payload = _sp\n"
        "def _write_count():\n"
        "    open(_os.path.join(_bd, 'count.' + str(_os.getpid())), 'w').write(str(_calls['n']))\n"
        f"_c = _Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})\n"
        f"_c._now = lambda: {_R16_RACE_CLOCK!r}\n"  # DETERMINISTIC claim clock -> exact expiry bound
        "def _cp(name):\n"
        "    if name == 'before_claim': _await_before_claim()\n"
        "    if name == 'after_claim_check_before_write': _await_claim2()\n"
        "    if name == 'after_claim_before_qdrant':\n"
        f"        _write_count(); os._exit({_R16_RACE_WIN})\n"
        "_c._checkpoint = _cp\n"
        "_c.reconcile_once()\n"
        f"_write_count(); os._exit({_R16_RACE_LOSE})\n"
    )


def _check_r16_race(base: Path) -> None:
    """Deterministic TWO-PROCESS claim race (Yua R16 item 2/3): two workers race the guarded claim on ONE
    due, unleased row. Exactly ONE claims (WIN) and one loses (LOSE); the durable claim is observed by the
    loser (its rowcount is 0). The row's exact snapshot shows only the winner's claim - one owner, PENDING,
    attempts 0, next_attempt NULL, no event/marker. select_then_update admits BOTH (two WINs)."""
    _api()  # xfail today
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "lifecycle.db"
    barrier_dir = base / "barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    collection = str(collection_for_plane("episodic"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _RefCoordinator(client=QdrantClient(":memory:"), db_path=db_path, mode=mode)  # schema
    opk = _fill_outbox(db_path, collection, pending=1, tag="race16")[
        0
    ]  # one due, unleased PENDING row
    fields = ("state", "attempts", "next_attempt_epoch", "lease_owner", "lease_expires_epoch")
    before = tuple(_outbox_field(db_path, opk, f) for f in fields)
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _r16_race_child_source(
                    mode=mode, db_path=db_path, opk=opk, barrier_dir=barrier_dir
                ),
            ]
        )
        for _ in range(2)
    ]
    codes = [p.wait(timeout=90) for p in procs]
    if sorted(codes) != [_R16_RACE_WIN, _R16_RACE_LOSE]:
        raise DefectStillPresent(
            f"exactly ONE worker must claim (WIN) and one lose (LOSE); got exit codes {codes} "
            f"(two {_R16_RACE_WIN} = both claimed = a non-atomic check-then-update)"
        )
    # exact per-child Qdrant attempt counts (measured, not proxied): both must be 0 (winner exits
    # pre-Qdrant; loser never claims). Plus exactly zero events.
    counts = [
        int(f.read_text().strip()) for f in barrier_dir.iterdir() if f.name.startswith("count.")
    ]
    if len(counts) != 2 or any(n != 0 for n in counts):
        raise DefectStillPresent(
            f"R16 race: each child must make EXACTLY zero Qdrant mutations; got counts {counts}"
        )
    con = sqlite3.connect(str(db_path))
    try:
        events = con.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
    finally:
        con.close()
    if events != 0:
        raise DefectStillPresent(
            "R16 the winner exits pre-Qdrant; no FINAL event may exist after the race"
        )
    # exact before/after 5-field snapshot: only the winner's claim changes owner+expiry; state/attempts/
    # next_attempt stay value-identical; owner is fresh/non-null; expiry is finite and inside the TTL window.
    after = tuple(_outbox_field(db_path, opk, f) for f in fields)
    if after[:3] != before[:3]:
        raise DefectStillPresent(
            f"R16 the loser must not change state/attempts/next_attempt: {before[:3]} -> {after[:3]}"
        )
    owner, exp = after[3], after[4]
    if owner is None or before[3] is not None:
        raise DefectStillPresent(
            "R16 after the race exactly the winner's fresh, non-null owner must be set"
        )
    # the claim used a DETERMINISTIC injected clock, so the winner's expiry must be EXACTLY
    # _R16_RACE_CLOCK + DEFAULT_LEASE_TTL - finite, exclusive (> claim time), and NOT one tick past the
    # configured TTL. lease_overshoot_ttl (expiry = now+TTL+0.5) fails this exact bound.
    want_expiry = _R16_RACE_CLOCK + DEFAULT_LEASE_TTL
    if exp is None or not math.isfinite(float(exp)) or abs(float(exp) - want_expiry) > 1e-6:
        raise DefectStillPresent(
            f"R16 the winner's lease expiry must be EXACTLY now+TTL ({want_expiry}), finite, not one tick "
            f"longer than the configured TTL; got {exp}"
        )


def _r18_setup(coord: Any, c: QdrantClient, poison: _Seed, others: list[tuple[_Seed, str]]) -> None:
    """Create durable PENDING rows for the poison + other objects (poison's op is 'op-P', inserted FIRST
    so it is the head-of-line row), then arm the poison so ONLY its object keeps failing transient."""
    _fail_set_payload(c, _TransientQdrantError("hold"))
    coord.transition(_intent(poison, to_state="matured", operation_key="op-P"))
    for obj, op in others:
        coord.transition(_intent(obj, to_state="matured", operation_key=op))
    _restore_set_payload(c)
    _fail_set_payload_for(c, poison.object_id, _TransientQdrantError("poison"))


def _check_r18(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """No poison-row starvation (Yua R18): one repeatedly-transient PENDING row must NOT block other due
    rows. Fairness is DUE-TIME ADVANCEMENT - a failed poison is rescheduled (not-due), so the due filter
    lets other rows through under both limit=1 (repeated calls) and limit>1 (one batch). The poison stays
    PENDING forever (R15 no-abandon), never dropped, and is processed through the normal claim/apply/fence
    (no lease/cap/version bypass). Each scenario is a fresh env with an injected clock."""
    base = Path(db_path).parent
    T = 5_000.0

    # (1) limit=1 across repeated reconciles: A and B must both finalize despite the head-of-line poison,
    #     and the poison stays PENDING (never abandoned/dropped).
    c, s, d = _make_env(base / "r18-limit1")
    try:
        coord = _coordinator(c, d)
        coord._now = lambda: T
        a, b = _seed_extra(c, "a"), _seed_extra(c, "b")
        _r18_setup(coord, c, s, [(a, "op-A"), (b, "op-B")])
        for _ in range(12):
            coord.reconcile_once(limit=1)
            if (
                _outbox_field(d, "op-A", "state") == "FINAL"
                and _outbox_field(d, "op-B", "state") == "FINAL"
            ):
                break
        if (
            _outbox_field(d, "op-A", "state") != "FINAL"
            or _outbox_field(d, "op-B", "state") != "FINAL"
        ):
            raise DefectStillPresent(
                "R18 a head-of-line poison row must NOT starve other due rows under limit=1 (A and B "
                "must both finalize)"
            )
        if _outbox_field(d, "op-P", "state") != "PENDING":
            raise DefectStillPresent(
                "R18 the poison row must stay PENDING (never falsely finalized / abandoned / dropped)"
            )
    finally:
        c.close()

    # (2) limit>1 single batch: a head-of-line poison must not block later rows in the SAME batch; the
    #     report accounts EXACTLY; the poison went through the normal R15 reschedule (no bypass).
    c, s, d = _make_env(base / "r18-batch")
    try:
        coord = _coordinator(c, d)
        coord._now = lambda: T
        a, b = _seed_extra(c, "a"), _seed_extra(c, "b")
        _r18_setup(coord, c, s, [(a, "op-A"), (b, "op-B")])
        rep = coord.reconcile_once(limit=10)
        if (
            _outbox_field(d, "op-A", "state") != "FINAL"
            or _outbox_field(d, "op-B", "state") != "FINAL"
        ):
            raise DefectStillPresent(
                "R18 a head-of-line poison row must NOT block later due rows in the same batch"
            )
        if _outbox_field(d, "op-P", "state") != "PENDING":
            raise DefectStillPresent("R18 the poison row must stay PENDING after the batch")
        if (rep.claimed, rep.finalized, rep.pending, rep.abandoned) != (3, 2, 1, 0):
            raise DefectStillPresent(
                f"R18 batch report must be claimed=3/finalized=2/pending=1/abandoned=0; got "
                f"{(rep.claimed, rep.finalized, rep.pending, rep.abandoned)}"
            )
        # no-bypass: the poison went through the normal R15 path (attempts incremented, rescheduled, lease
        # released) rather than being dropped or spun without backoff.
        p_next = _outbox_field(d, "op-P", "next_attempt_epoch")
        if _outbox_field(d, "op-P", "attempts") != 1 or p_next is None or float(p_next) <= T:
            raise DefectStillPresent(
                "R18 the poison must be retried via the normal R15 reschedule (attempts+1, backoff in the "
                "future, no bypass)"
            )
        if _outbox_field(d, "op-P", "lease_owner") is not None:
            raise DefectStillPresent(
                "R18 the poison's lease must be released after its transient failure"
            )
    finally:
        c.close()


def _check_r19(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """PII-free content + bounded observability (Yua R19): the outbox persists ONLY the canonical minimal
    target patch (state/version/lineage) + matching SHA - no arbitrary payload/unknown keys; and the
    coordinator NEVER emits operation/object/namespace/content/patch/reason/token into logs or metric
    labels. Metrics are a no-label pending gauge + a failures counter whose only label is class in
    {terminal, transient}. Adversarial PII sentinels are injected into the operation_key, the intent
    namespace/reason, and the exception message, then checked across the row, the captured log, and the
    Prometheus exposition. R13 full-readback SHA and R15 unknown classification (without leaking reason)
    are preserved."""
    base = Path(db_path).parent
    c, s, d = _make_env(base / "r19")
    op = f"{_PII_SENTINEL}-op"
    reason = f"{_PII_SENTINEL}-reason"
    # a seed carrying a PII namespace (points at the real object; apply keys on object_id+version).
    poison = _Seed(
        collection=s.collection,
        object_id=s.object_id,
        namespace=f"{_PII_SENTINEL}-ns",
        version=s.version,
    )
    cap = _LogCapture()
    _LIFECYCLE_LOGGER.addHandler(cap)
    old_level = _LIFECYCLE_LOGGER.level
    _LIFECYCLE_LOGGER.setLevel(logging.DEBUG)
    try:
        coord = _coordinator(c, d)
        _fail_set_payload(c, _TransientQdrantError("hold"))
        coord.transition(_intent(poison, to_state="matured", operation_key=op, reason=reason))
        _restore_set_payload(c)
        _fail_set_payload(
            c, _UnknownQdrantError(f"{_PII_SENTINEL}-exc")
        )  # an UNKNOWN failure w/ PII msg
        coord.reconcile_once(limit=10)  # -> _observe_failure + _observe_pending
    finally:
        _LIFECYCLE_LOGGER.removeHandler(cap)
        _LIFECYCLE_LOGGER.setLevel(old_level)

    # ROW: the persisted patch must be EXACTLY the canonical _intended_patch for this intent (keys AND
    # values, not merely a subset - so {} or a missing required field is rejected), with the canonical
    # serialization and the canonical SHA (R13), and no PII.
    expected_patch = _intended_patch(
        _intent(poison, to_state="matured", operation_key=op, reason=reason)
    )
    row = _outbox_rows(d, op)[0]
    patch = json.loads(str(row["patch_json"]))
    if patch != expected_patch:
        raise DefectStillPresent(
            f"R19 the persisted patch must be EXACTLY the canonical intended patch {expected_patch}; "
            f"got {patch}"
        )
    if str(row["patch_json"]) != json.dumps(expected_patch, sort_keys=True, separators=(",", ":")):
        raise DefectStillPresent("R19 the stored patch_json must be the CANONICAL serialization")
    if str(row["patch_sha"]) != _canonical_patch_sha(expected_patch):
        raise DefectStillPresent(
            "R19 the stored patch_sha must be the canonical SHA of the intended patch (R13 preserved)"
        )
    if _PII_SENTINEL in str(row["patch_json"]):
        raise DefectStillPresent("R19 no PII may appear in the persisted patch")

    # LOG: no captured record may carry any PII (message OR structured fields).
    for r in cap.records:
        blob = f"{r.getMessage()} {r.__dict__}"
        if _PII_SENTINEL in blob:
            raise DefectStillPresent(f"R19 no PII may appear in a log record: {blob[:120]}")

    # METRICS: emission is REQUIRED (exactly one pending gauge + one failure delta, correct values), every
    # NAME is whitelisted and PII-free, the pending gauge is UNLABELED, and the failures counter has
    # EXACTLY class in {terminal, transient}. Same for the rendered Prometheus exposition.
    for name, labels, _value in coord._metrics:
        if name not in _ALLOWED_METRIC_NAMES or _PII_SENTINEL in name:
            raise DefectStillPresent(
                f"R19 metric name must be in the whitelist {sorted(_ALLOWED_METRIC_NAMES)}, PII-free; got {name!r}"
            )
        if name == "musubi_lifecycle_outbox_pending":
            if labels:
                raise DefectStillPresent(f"R19 the pending gauge must be UNLABELED, got {labels}")
        elif set(labels.keys()) != {"class"}:
            raise DefectStillPresent(
                f"R19 the failures metric must have EXACTLY a 'class' label (no object/namespace); "
                f"got {set(labels.keys())}"
            )
        elif labels["class"] not in ("terminal", "transient"):
            raise DefectStillPresent(
                f"R19 the metric class label must be bounded to terminal|transient; got {labels['class']!r}"
            )
        if any(_PII_SENTINEL in str(v) for v in labels.values()):
            raise DefectStillPresent("R19 no PII may appear in a metric label")
    pending = [m for m in coord._metrics if m[0] == "musubi_lifecycle_outbox_pending"]
    failures = [
        m for m in coord._metrics if m[0] == "musubi_lifecycle_outbox_mutation_failures_total"
    ]
    if len(pending) != 1:
        raise DefectStillPresent(
            f"R19 exactly ONE pending gauge sample expected, got {len(pending)}"
        )
    if len(failures) != 1:
        raise DefectStillPresent(
            f"R19 exactly ONE failure counter delta expected, got {len(failures)}"
        )
    if pending[0][2] != 1.0:
        raise DefectStillPresent(
            f"R19 the pending gauge must be 1 (the poison row), got {pending[0][2]}"
        )
    if failures[0][2] != 1.0:
        raise DefectStillPresent(f"R19 the failure counter delta must be 1, got {failures[0][2]}")
    if _PII_SENTINEL in coord._prometheus_exposition():
        raise DefectStillPresent("R19 no PII may appear in the Prometheus exposition")
    c.close()


def _check_r9(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """Idempotent replay: a PENDING op processed, then REPLAYED (duplicate delivery), yields EXACTLY ONE
    FINAL, ONE audit event, and ONE EFFECTIVE apply (measured by set_payload calls, not readback), leaving
    the object at target/v+1 - never a second event or a second mutation."""
    coord = _coordinator(client, db_path)
    _fail_set_payload(client, _TransientQdrantError("hold op-r9 PENDING"))
    r1 = coord.transition(_intent(seed, to_state="matured", operation_key="op-r9"))
    if not isinstance(r1, Ok):
        raise DefectStillPresent("op-r9 must be Ok(Pending) after a transient failure")
    _restore_set_payload(client)
    calls = _count_set_payload(client)
    # first reconcile: exactly one effective apply -> FINAL + one event.
    n0 = calls["n"]
    coord.reconcile_once(limit=10)
    if calls["n"] - n0 != 1:
        raise DefectStillPresent(
            f"the first reconcile must apply exactly once, got {calls['n'] - n0}"
        )
    rows = _outbox_for_object(db_path, seed.object_id)
    if len(rows) != 1 or rows[0]["state"] != "FINAL":
        raise DefectStillPresent("the op must be FINAL after the first reconcile")
    ev = rows[0]["event_id"]
    if _final_event_count(db_path, ev) != 1 or _events_for_object(db_path, seed.object_id) != 1:
        raise DefectStillPresent("the first reconcile must produce exactly one event")
    # REPLAY: a duplicate delivery re-PENDINGs the same op; a fresh reconcile must be exactly-once.
    _reset_to_pending(db_path, "op-r9")
    n1 = calls["n"]
    coord.reconcile_once(limit=10)
    if calls["n"] - n1 != 0:
        raise DefectStillPresent(
            f"a replayed PENDING must NOT re-apply (idempotent), got {calls['n'] - n1} applies"
        )
    if _events_for_object(db_path, seed.object_id) != 1:
        raise DefectStillPresent(
            "a replayed op must NOT emit a second audit event (event_id idempotent)"
        )
    rows2 = _outbox_for_object(db_path, seed.object_id)
    if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
        raise DefectStillPresent("a replayed op must resolve to exactly one FINAL row")
    # the replay must not re-key: the FINAL row still references the ORIGINAL event, and that event is
    # still exactly one (Yua/Tama: prove the stable-event property, not just a total count).
    if rows2[0]["event_id"] != ev:
        raise DefectStillPresent(
            "a replay must leave the FINAL row referencing the ORIGINAL event_id"
        )
    if _final_event_count(db_path, ev) != 1:
        raise DefectStillPresent("the ORIGINAL event_id must remain exactly one event after replay")
    if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
        raise DefectStillPresent("a replayed op must NOT mutate the object a second time")


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
    # snapshot BEFORE the colliding intent_B, then assert it changed NOTHING (Yua completeness: not just
    # Err + zero apply, but zero new outbox row, zero new audit event, unchanged object).
    before_calls = calls["n"]
    rows_before = len(_outbox_for_object(db_path, seed.object_id))
    events_before = _events_for_object(db_path, seed.object_id)
    state_before = _qdrant_state(client, seed.collection, seed.object_id)
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
    if len(_outbox_for_object(db_path, seed.object_id)) != rows_before:
        raise DefectStillPresent(
            "the delimiter-collision conflict must NOT create a new outbox row"
        )
    if _events_for_object(db_path, seed.object_id) != events_before:
        raise DefectStillPresent(
            "the delimiter-collision conflict must NOT create a new audit event"
        )
    if _qdrant_state(client, seed.collection, seed.object_id) != state_before:
        raise DefectStillPresent("the delimiter-collision conflict must NOT change the object")


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


_R14_RACE_WIN = (
    51  # won admission (its row committed; it exits at after_pending_commit, before apply)
)
_R14_RACE_CAP = 52  # was cap_exceeded AND touched no Qdrant (the correct loser)
_R14_RACE_TOUCHED = 58  # cap_exceeded BUT it had already touched Qdrant - a defect


def _r14_two_barrier_source(barrier_dir: Path) -> str:
    """Two file barriers for the R14 admission race: _await_begin (both processes REACH admission) and
    _await_cap2 (both have COUNTED before either inserts - this forces the non-atomic check_then_insert
    candidate to deterministically admit both, while the correct BEGIN IMMEDIATE serializes regardless)."""

    def one(fn: str, tag: str) -> str:
        return (
            f"def {fn}():\n"
            f"    open(_os.path.join(_bd, {tag!r} + '.' + str(_os.getpid())), 'w').close()\n"
            f"    for _ in range(int({_RACE_BARRIER_TIMEOUT!r} / 0.02)):\n"
            f"        if len([f for f in _os.listdir(_bd) if f.startswith({tag!r})]) >= 2: return\n"
            "        _time.sleep(0.02)\n"
            "    raise SystemExit('barrier timeout')\n"
        )

    return (
        "import os as _os, time as _time\n"
        f"_bd = {str(barrier_dir)!r}\n" + one("_await_begin", "begin") + one("_await_cap2", "cap2")
    )


def _r14_race_child_source(
    *, mode: str, cap: int, db_path: Path, seed: _Seed, target: str, op_key: str, barrier_dir: Path
) -> str:
    """A child that races admission at cap-1 with a DISTINCT object. It exits WIN at after_pending_commit
    (row committed, before any apply), or - if the atomic cap gate rejects it - CAP (verifying via a
    wrapped set_payload that it touched NO Qdrant). Two WINs means the cap was lost."""
    return (
        "import os, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord, _RefIntent as _Intent\n"
        + _r14_two_barrier_source(barrier_dir)
        + "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    _client = QdrantClient(':memory:')\n"
        "_touched = {'n': 0}\n"
        "_orig_sp = _client.set_payload\n"
        "def _sp(*a, **k):\n"
        "    _touched['n'] += 1\n"
        "    return _orig_sp(*a, **k)\n"
        "_client.set_payload = _sp\n"
        f"_c = _Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r}, pending_cap={cap})\n"
        "def _cp(name):\n"
        "    if name == 'before_pending_commit': _await_begin()\n"
        "    if name == 'after_cap_count_before_insert': _await_cap2()\n"
        f"    if name == 'after_pending_commit': os._exit({_R14_RACE_WIN})\n"
        "_c._checkpoint = _cp\n"
        f"_res = _c.transition(_Intent(collection={seed.collection!r}, object_id={seed.object_id!r}, "
        f"namespace={seed.namespace!r}, expected_version={seed.version}, target_state={target!r}, "
        f"actor='t', reason='r', operation_key={op_key!r}))\n"
        "code = getattr(getattr(_res, 'error', None), 'code', None)\n"
        "if code == 'cap_exceeded':\n"
        f"    os._exit({_R14_RACE_CAP} if _touched['n'] == 0 else {_R14_RACE_TOUCHED})\n"
        "os._exit(99)\n"
    )


def _check_r14_race(base: Path) -> None:
    """Deterministic TWO-PROCESS admission race from cap-1 with DISTINCT objects (Yua R14 (e)): exactly
    ONE admission + ONE cap_exceeded, the shared backlog settles at EXACTLY the cap, and the rejected
    process touched no Qdrant. The non-atomic check_then_insert candidate admits both -> cap+1."""
    _api()  # xfail today (coordinator absent)
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "lifecycle.db"
    barrier_dir = base / "barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    collection = str(collection_for_plane("episodic"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _RefCoordinator(
            client=QdrantClient(":memory:"), db_path=db_path, mode=mode, pending_cap=_R14_CAP
        )  # create schema
    _fill_outbox(db_path, collection, pending=_R14_CAP - 1, tag="racefill")  # backlog at cap-1
    seeds = (
        _Seed(
            collection=collection, object_id="raceobjA000000000000000000", namespace=_NS, version=1
        ),
        _Seed(
            collection=collection, object_id="raceobjB000000000000000000", namespace=_NS, version=1
        ),
    )
    op_keys = ("op-a14", "op-b14")
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _r14_race_child_source(
                    mode=mode,
                    cap=_R14_CAP,
                    db_path=db_path,
                    seed=seeds[i],
                    target="matured",
                    op_key=op_keys[i],
                    barrier_dir=barrier_dir,
                ),
            ]
        )
        for i in range(2)
    ]
    codes = [p.wait(timeout=90) for p in procs]
    if _R14_RACE_TOUCHED in codes:
        raise DefectStillPresent("a cap_exceeded loser must have touched NO Qdrant (0 set_payload)")
    if sorted(codes) != [_R14_RACE_WIN, _R14_RACE_CAP]:
        raise DefectStillPresent(
            f"from cap-1, exactly ONE admission + ONE cap_exceeded expected; got exit codes {codes} "
            f"(two {_R14_RACE_WIN} = both admitted over the cap = the check-then-insert race)"
        )
    total = _nonterminal_total(db_path)
    if total != _R14_CAP:
        raise DefectStillPresent(
            f"the shared backlog must settle at EXACTLY the cap ({_R14_CAP}); got {total} (cap+1 = lost cap)"
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


_R9_REASON = (
    "today there is no outbox/reconciler and no stable per-op event_id, so a replayed/duplicate delivery "
    "re-applies and re-emits; R9 needs replay to be exactly-once: one FINAL, one event, one effective apply."
)
_R13_REASON = (
    "today an apply is confirmed on version (and maybe state) alone, so a partial/corrupt mutation that "
    "drops a lineage field is falsely accepted; R13 needs a FULL readback patch SHA over the intended key "
    "set projected from ACTUAL data - a missing/wrong field must refuse (Ok(Pending), no marker/event/FINAL)."
)
_R14_REASON = (
    "today the outbox backlog is unbounded, so a stalled reconciler lets in-flight intents grow without "
    "limit; R14 needs a hard cap on GLOBAL non-terminal rows enforced atomically at admission - at the "
    "cap a fresh begin returns Err(cap_exceeded), Qdrant untouched, no row/event/marker."
)
_R15_REASON = (
    "today a retried transient failure can be given up on (abandoned) by attempt count, losing a durable "
    "intent; R15 needs transient/unknown failures kept PENDING forever with a durable attempts count "
    "(observability only) + bounded overflow-safe backoff - abandoned ONLY on proven-terminal, never by count."
)
_R16_REASON = (
    "today nothing leases a reconciliation row, so two workers can process the same intent concurrently; "
    "R16 needs an atomic guarded claim (one committed UPDATE, rowcount==1 = ownership) of a due, "
    "unleased-or-expired row, an exclusive unexpired owner, and owner-guarded dispositions that clear the "
    "lease atomically - `claimed` counts successful claims, and lease_ttl is positive-finite."
)
_R17_REASON = (
    "today nothing reclaims a dead worker's lease, and nothing stops a stale/expired owner from clobbering "
    "a reclaimer; R17 needs the exact expiry boundary (expires>now valid, <=now reclaimable), a FRESH "
    "reclaim token so an old token can never ABA-match, and the composition where the owner guard blocks a "
    "stale disposition while the R13/R22 version fence makes a stale apply a zero-match no-op."
)
_R18_REASON = (
    "today a repeatedly-transient 'poison' row at the head of the reconcile queue can be reselected every "
    "pass and starve other due rows; R18 needs due-time advancement (a failed poison is rescheduled "
    "not-due so the due filter lets others through) under both limit=1 and a limit>1 batch, exact report "
    "accounting, and no head-of-line break or false-success drop - the poison stays PENDING forever."
)
_R19_REASON = (
    "today the outbox could persist arbitrary payload and the coordinator could log/label PII; R19 needs "
    "the row to store ONLY the canonical minimal patch + matching SHA, and every log/metric to carry only "
    "stable codes + a bounded class label - never operation/object/namespace/content/patch/reason/token."
)
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


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R9_REASON)
def test_r9_idempotent_replay(env: tuple[QdrantClient, _Seed, Path]) -> None:
    _check_r9(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R13_REASON)
def test_r13_conditional_apply_full_readback_patch_sha(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r13(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R14_REASON)
def test_r14_hard_pending_cap_admission_backpressure(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r14(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R15_REASON)
def test_r15_transient_never_abandoned_by_attempt_count(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r15(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R16_REASON)
def test_r16_valid_lease_exclusive_processing(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r16(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R17_REASON)
def test_r17_expired_owner_reclaim_safe(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r17(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R18_REASON)
def test_r18_no_poison_row_starvation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r18(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R19_REASON)
def test_r19_pii_free_content_and_bounded_observability(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r19(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R10_REASON)
def test_r10_operation_key_idempotent_across_caller_retries(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r10(*env)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R11_REASON)
def test_r11_single_active_intent_per_object(tmp_path: Path) -> None:
    _check_r11(tmp_path)


_R14_RACE_REASON = (
    "today admission is not atomic across processes, so two concurrent begins at cap-1 both count under "
    "the cap and both insert (a check-then-insert race) -> the backlog exceeds the cap; R14 needs BEGIN "
    "IMMEDIATE -> count -> insert -> commit in one transaction so exactly one admits and one is cap_exceeded."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R14_RACE_REASON)
def test_r14_two_process_admission_race_holds_cap(tmp_path: Path) -> None:
    _check_r14_race(tmp_path)


_R16_RACE_REASON = (
    "today a reconciliation claim is not atomic across processes, so two workers can both check a row as "
    "claimable and both take it (a check-then-update race) -> concurrent double-processing; R16 needs ONE "
    "committed guarded UPDATE where rowcount==1 is ownership, so exactly one worker claims and one loses."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R16_RACE_REASON)
def test_r16_two_process_claim_race_one_owner(tmp_path: Path) -> None:
    _check_r16_race(tmp_path)


_R17_RECLAIM_REASON = (
    "today a dead worker's lease is never reclaimed and a crash-applied mutation has no owner to finalize "
    "it; R17 needs a new owner to reclaim the expired lease, READBACK-CONFIRM the already-applied mutation, "
    "and finalize it exactly once WITHOUT a second effective apply - proven with a real process that dies "
    "over an on-disk Qdrant."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R17_RECLAIM_REASON)
def test_r17_crash_reclaim_readback_confirms_no_reapply(tmp_path: Path) -> None:
    _check_r17_reclaim(tmp_path)


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


_R17_CRASH_CODE = (
    71  # worker A crashed at after_qdrant_before_applied (Qdrant mutated, row PENDING+leased)
)


def _r17_crash_child_source(
    *,
    use_reference: bool,
    mode: str,
    qdrant_path: Path,
    db_path: Path,
    crash_at: str,
    exit_code: int,
) -> str:
    """Worker A: reconcile the pre-existing PENDING row (claim -> apply Qdrant), then die at `crash_at`
    (over an ON-DISK Qdrant) so the mutation is a REAL surviving cross-process side effect and the row is
    left PENDING + still leased by the dead A."""
    if use_reference:
        imp = "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord"
        ctor = f"_Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})"
    else:  # future: the REAL src coordinator (absent today -> the parent xfails at _api())
        imp = "from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator as _Coord"
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
        "_c.reconcile_once()\n"
        "os._exit(99)\n"  # reached only if the crash checkpoint did NOT fire
    )


def _check_r17_reclaim(base: Path) -> None:
    """Expired-owner reclaim across a REAL crash (Yua R17): worker A claims + applies Qdrant then DIES
    (os._exit over an ON-DISK Qdrant - a genuine surviving process, not an in-memory fake) before marking
    APPLIED. The mutation is durable; the row is PENDING + still leased by the dead A. After the lease
    expires, worker B reclaims, READBACK-CONFIRMS the already-applied mutation, and finalizes WITHOUT a
    second effective apply. reclaim_reapplies_after_readback re-applies -> a false version fence -> the
    audit is lost (ABANDONED, not FINAL) and a wasted Qdrant attempt is made."""
    _api()  # xfail today (coordinator absent); a candidate is active under red-proof
    use_ref = _ACTIVE_CANDIDATE is not None
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    seed, db_path, qdrant_path = _make_ondisk_env(base)
    # the parent creates A's durable PENDING intent, then releases the Qdrant lock for the child.
    client = _open_ondisk_qdrant(qdrant_path)
    coord = _coordinator(client, db_path)
    _fail_set_payload(client, _TransientQdrantError("hold PENDING"))
    coord.transition(_intent(seed, to_state="matured", operation_key="op-r17"))
    _restore_set_payload(client)
    client.close()
    src = _r17_crash_child_source(
        use_reference=use_ref,
        mode=mode,
        qdrant_path=qdrant_path,
        db_path=db_path,
        crash_at="after_qdrant_before_applied",
        exit_code=_R17_CRASH_CODE,
    )
    proc = subprocess.run([sys.executable, "-c", src], capture_output=True, timeout=90, check=False)
    if proc.returncode != _R17_CRASH_CODE:
        raise DefectStillPresent(
            f"worker A must crash at after_qdrant_before_applied with code {_R17_CRASH_CODE} (never "
            f"timeout/kill), got {proc.returncode}: {proc.stderr.decode()[-300:]}"
        )
    rows = _outbox_rows(db_path, "op-r17")
    if len(rows) != 1 or rows[0]["state"] != "PENDING":
        raise DefectStillPresent(
            f"a crash post-Qdrant/pre-APPLIED must leave ONE PENDING row, got {[r['state'] for r in rows]}"
        )
    if _outbox_field(db_path, "op-r17", "lease_owner") is None:
        raise DefectStillPresent(
            "the dead worker A's lease must still be HELD (reclaimable after expiry)"
        )
    client = _open_ondisk_qdrant(qdrant_path)
    try:
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent(
                "A's Qdrant mutation must be durable BEFORE reclaim (the ACTUAL cross-process side effect)"
            )
        _set_outbox(
            db_path, "op-r17", lease_expires_epoch=0.0
        )  # A's lease expires -> B may reclaim
        coord = _coordinator(client, db_path)
        calls = _count_set_payload(client)
        n0 = calls["n"]
        report = coord.reconcile_once(limit=10)  # B: reclaim -> readback-confirm -> finalize
        if calls["n"] - n0 != 0:
            raise DefectStillPresent(
                f"B must READBACK-CONFIRM the already-applied mutation, NOT re-apply; got "
                f"{calls['n'] - n0} Qdrant attempt(s)"
            )
        rows2 = _outbox_rows(db_path, "op-r17")
        if len(rows2) != 1 or rows2[0]["state"] != "FINAL":
            raise DefectStillPresent(
                f"B's reclaim must FINALIZE the recovered mutation (not lose it), got "
                f"{[r['state'] for r in rows2]}"
            )
        if _final_event_count(db_path, rows2[0]["event_id"]) != 1:
            raise DefectStillPresent("the reclaim must produce EXACTLY ONE FINAL event")
        if getattr(report, "finalized", None) != 1:
            raise DefectStillPresent(f"report must show one finalized, got {report!r}")
        if _qdrant_state(client, seed.collection, seed.object_id) != (seed.version + 1, "matured"):
            raise DefectStillPresent(
                "B must NOT mutate Qdrant again (exactly one effective mutation)"
            )
        if _outbox_field(db_path, "op-r17", "lease_owner") is not None:
            raise DefectStillPresent("a finalized row must have its lease cleared")
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


# ---- R22: two DIFFERENT transitions race on one object, TWO REAL PROCESSES (Yua R22 rebuild) ------- #
#
# Yua rejected the sequential version. This is a real race: two OS processes with DIFFERENT operation
# keys, DIFFERENT targets, the SAME expected_version, on ONE object, over a SHARED ON-DISK Qdrant + SQLite,
# synchronized at before_pending_commit. Exactly one wins the atomic begin (partial unique index) and
# ACTUALLY mutates the on-disk Qdrant (the winner lazy-opens it). The loser is rejected either at begin
# (active_intent_exists - never opens Qdrant) OR at the version fence (version_fence_violation - it DOES
# lazy-open Qdrant and issues a zero-match conditional attempt, so its readback shows the winner's target
# and it never mutates). Each child exits with an op-DISTINCT winner code so the parent correlates the
# winning op to the single FINAL row, its audit event to_state, the single effective-apply marker, and the
# exact Qdrant target/version - a loser that overwrote-then-abandoned while the other finalized cannot
# pass. Wrong candidate: non_atomic_cas ONLY (index off AND fence off -> the real lost update / two
# effective applies). R11 owns the naive-check-then-insert (no_unique_index) proof; in R22 the index and
# the fence are belt-and-suspenders, so removing only the index is caught by the fence and is not an R22
# discriminator.

_R22_WIN_A = 31  # op-a22 (-> matured) won
_R22_WIN_B = 34  # op-b22 (-> demoted) won
_R22_CONFLICT = 32
_R22_FENCE = 33


def _r22_child_source(
    *,
    mode: str,
    db_path: Path,
    qdrant_path: Path,
    seed: _Seed,
    target: str,
    op_key: str,
    win_code: int,
    barrier_dir: Path,
) -> str:
    return (
        "import os, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from musubi.types.common import Ok\n"
        "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord, _RefIntent as _Intent\n"
        + _barrier_source(barrier_dir, "begin", 2)
        + f"_c = _Coord(db_path={str(db_path)!r}, qdrant_path={str(qdrant_path)!r}, mode={mode!r})\n"
        "def _cp(name):\n"
        "    if name == 'before_pending_commit': _await_barrier()\n"
        "_c._checkpoint = _cp\n"
        f"_res = _c.transition(_Intent(collection={seed.collection!r}, object_id={seed.object_id!r}, "
        f"namespace={seed.namespace!r}, expected_version={seed.version}, target_state={target!r}, "
        f"actor='t', reason='r', operation_key={op_key!r}))\n"
        "code = getattr(getattr(_res, 'error', None), 'code', None)\n"
        f"os._exit({win_code} if isinstance(_res, Ok) else "
        f"({_R22_CONFLICT} if code in ('active_intent_exists', 'operation_key_conflict') else "
        f"({_R22_FENCE} if code == 'version_fence_violation' else 99)))\n"
    )


def _check_r22(base: Path) -> None:
    _api()  # xfail today (coordinator absent)
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    seed, db_path, qdrant_path = _make_ondisk_env(
        base
    )  # seeds object v1/provisional, closes Qdrant
    barrier_dir = base / "barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    _RefCoordinator(
        db_path=db_path, qdrant_path=qdrant_path, mode=mode
    )  # create outbox schema+index
    # op-a22 -> matured (win code _R22_WIN_A); op-b22 -> demoted (win code _R22_WIN_B).
    specs = (("matured", "op-a22", _R22_WIN_A), ("demoted", "op-b22", _R22_WIN_B))
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _r22_child_source(
                    mode=mode,
                    db_path=db_path,
                    qdrant_path=qdrant_path,
                    seed=seed,
                    target=target,
                    op_key=op_key,
                    win_code=win_code,
                    barrier_dir=barrier_dir,
                ),
            ]
        )
        for target, op_key, win_code in specs
    ]
    codes = [p.wait(timeout=120) for p in procs]
    # EXACTLY ONE winner (identified by its op-distinct code); the loser is rejected at begin
    # (active_intent_exists) OR at the fence (version_fence_violation) - both valid "cannot overwrite".
    wins = [c for c in codes if c in (_R22_WIN_A, _R22_WIN_B)]
    losers = [c for c in codes if c in (_R22_CONFLICT, _R22_FENCE)]
    if len(wins) != 1 or len(losers) != 1:
        raise DefectStillPresent(
            f"exactly one transition must WIN + mutate and the other be fenced/conflict-rejected; got "
            f"exit codes {codes} (two winners = the lost-update / double-operation race)"
        )
    winner_op, winner_target = (
        ("op-a22", "matured") if wins[0] == _R22_WIN_A else ("op-b22", "demoted")
    )
    # CORRELATE the winning op to the single FINAL row, its event, its effective-apply marker, and Qdrant.
    rows = _outbox_for_object(db_path, seed.object_id)
    finals = [r for r in rows if r["state"] == "FINAL"]
    if len(finals) != 1:  # a loser may leave an ABANDONED row, but NEVER a second FINAL
        raise DefectStillPresent(
            f"exactly ONE FINAL outbox row must exist, got {[(r['operation_key'], r['state']) for r in rows]}"
        )
    if finals[0]["operation_key"] != winner_op or finals[0]["target_state"] != winner_target:
        raise DefectStillPresent(
            f"the single FINAL must belong to the WINNING op {winner_op}->{winner_target}, "
            f"got {finals[0]['operation_key']}->{finals[0]['target_state']}"
        )
    if _events_for_object(db_path, seed.object_id) != 1:
        raise DefectStillPresent("exactly ONE audit event must exist for the object")
    if _event_to_state(db_path, finals[0]["event_id"]) != winner_target:
        raise DefectStillPresent("the single audit event's to_state must equal the winner's target")
    markers = _apply_markers(db_path, seed.object_id)
    if markers != [(winner_op, winner_target)]:
        raise DefectStillPresent(
            f"exactly ONE effective-apply success must exist and match the winner "
            f"{(winner_op, winner_target)}; got {markers} (two = the lost-update)"
        )
    client = _open_ondisk_qdrant(qdrant_path)
    try:
        if _qdrant_state(client, seed.collection, seed.object_id) != (
            seed.version + 1,
            winner_target,
        ):
            raise DefectStillPresent(
                f"Qdrant must equal the EXACT winner target {winner_target}/v{seed.version + 1}"
            )
    finally:
        client.close()


_R22_REASON = (
    "today two different transitions on one object can both apply (lost update / double operation); R22 "
    "needs exactly one winner that mutates while the loser is atomically fenced/conflict-rejected, one "
    "FINAL row, one event, no overwrite - proven with two real processes racing the begin boundary."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R22_REASON)
def test_r22_two_process_race_one_winner_mutates_loser_fenced(tmp_path: Path) -> None:
    _check_r22(tmp_path)


# ---- R20: rollback-refuses-nonterminal + maintenance lifecycle + terminal-row cleanup -------------- #


def _err_code(res: Any) -> Any:
    return getattr(getattr(res, "error", None), "code", None)


def _check_r20(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """R20 in-memory contract (Yua v3 + 5 corrections): rollback refuses while ANY nonterminal row exists
    (dropping nothing, leaving maintenance ACTIVE), quiesces admission durably, fences on a stale
    generation, holds the EX barrier through the deploy handoff, stays quiesced on handoff failure, and
    the terminal-row cleanup is a bounded, deterministic, atomic CTE that preserves young / NULL-age /
    every nonterminal row. Each sub-scenario runs on its own isolated DB (its own maintlock)."""
    base = db_path.parent
    coll = seed.collection

    def _bogus(opk: str) -> Any:
        return _api().TransitionIntent(
            collection=coll,
            object_id="r20bogus0000000000000000000",
            namespace=_NS,
            expected_version=1,
            target_state="matured",
            actor="t",
            reason="r",
            operation_key=opk,
        )

    # (A) refuse-on-nonterminal: a live PENDING intent blocks the drop; maintenance STAYS active.
    dba = base / "r20_refuse.db"
    coord = _coordinator(client, dba)
    _seed_outbox_row(
        dba, opk="p1", oid="obj-A0000000000000000000000", collection=coll, state="PENDING"
    )
    res = coord.rollback(expected_generation=_control_generation(dba) + 1)
    if _err_code(res) != "rollback_refused_nonterminal":
        raise DefectStillPresent(
            f"rollback must REFUSE while a nonterminal row exists, got {res!r}"
        )
    if not _table_exists(dba, "lifecycle_outbox") or "p1" not in _outbox_states(dba):
        raise DefectStillPresent(
            "a refused rollback must drop NOTHING (the live intent must survive)"
        )
    if not _control_active(dba):
        raise DefectStillPresent("a refused rollback must LEAVE maintenance active (correction 4)")

    # (B) lifecycle: active maintenance refuses new admission; abort_maintenance clears it.
    coord2 = _coordinator(client, dba)
    admit = coord2.transition(_bogus("op-admitB"))
    if _err_code(admit) != "maintenance_active":
        raise DefectStillPresent(
            f"admission during active maintenance must Err(maintenance_active), got {admit!r}"
        )
    aborted = coord2.abort_maintenance(expected_generation=_control_generation(dba))
    if not isinstance(aborted, Ok) or _control_active(dba):
        raise DefectStillPresent(f"abort_maintenance must clear the window, got {aborted!r}")

    # (C) generation fence: a stale expected_generation refuses and drops nothing.
    dbc = base / "r20_gen.db"
    coord = _coordinator(client, dbc)
    _seed_outbox_row(
        dbc,
        opk="t1",
        oid="obj-C0000000000000000000000",
        collection=coll,
        state="FINAL",
        terminal_epoch=1.0,
    )
    _set_control(dbc, generation=5)  # a competing rollback already advanced the generation
    res = coord.rollback(expected_generation=1)
    if _err_code(res) != "rollback_refused_stale_generation":
        raise DefectStillPresent(f"a stale expected_generation must refuse, got {res!r}")
    if not _table_exists(dbc, "lifecycle_outbox"):
        raise DefectStillPresent("a generation-fenced rollback must drop NOTHING")

    # (D) the check-to-lock race: maintenance activated inside the admission window must still be caught
    # by the post-lock recheck (a stale pre-lock check would slip the reader in past the drain).
    dbd = base / "r20_checkorder.db"
    coord = _coordinator(client, dbd)

    def _activate(name: str) -> None:
        if name in ("shared_lease_acquired", "active_checked_before_lock"):
            _set_control(dbd, active=1)

    coord._checkpoint = _activate
    res = coord.transition(_bogus("op-D"))
    if _err_code(res) != "maintenance_active":
        raise DefectStillPresent(
            f"maintenance activated in the check-to-lock window must be caught by the post-lock recheck "
            f"(Err maintenance_active), got {res!r}"
        )

    # (E) success: only-terminal rows -> DROP; the EX barrier is HELD through the deploy handoff; cutover
    # clears maintenance.
    dbe = base / "r20_success.db"
    coord = _coordinator(client, dbe)
    _seed_outbox_row(
        dbe,
        opk="t1",
        oid="obj-E1000000000000000000000",
        collection=coll,
        state="FINAL",
        terminal_epoch=1.0,
    )
    _seed_outbox_row(
        dbe,
        opk="t2",
        oid="obj-E2000000000000000000000",
        collection=coll,
        state="ABANDONED",
        terminal_epoch=2.0,
    )
    probe: dict[str, bool | None] = {"free": None}

    def _handoff() -> bool:
        probe["free"] = _probe_barrier_free(coord._maintlock)
        return True

    coord._deploy_handoff = _handoff
    res = coord.rollback(expected_generation=_control_generation(dbe) + 1)
    if not isinstance(res, Ok):
        raise DefectStillPresent(f"rollback over only-terminal rows must succeed, got {res!r}")
    if _table_exists(dbe, "lifecycle_outbox"):
        raise DefectStillPresent("a successful rollback must DROP lifecycle_outbox")
    if probe["free"] is not False:
        raise DefectStillPresent(
            "the EX barrier must be HELD through the deploy handoff (no admission before cutover)"
        )
    if _control_active(dbe):
        raise DefectStillPresent("a successful rollback must clear maintenance at cutover")

    # (F) handoff failure: stay QUIESCED, no auto-resume.
    dbf = base / "r20_handoff.db"
    coord = _coordinator(client, dbf)
    _seed_outbox_row(
        dbf,
        opk="t1",
        oid="obj-F1000000000000000000000",
        collection=coll,
        state="FINAL",
        terminal_epoch=1.0,
    )
    coord._deploy_handoff = lambda: False
    res = coord.rollback(expected_generation=_control_generation(dbf) + 1)
    if _err_code(res) != "handoff_failed":
        raise DefectStillPresent(f"a failed deploy handoff must Err(handoff_failed), got {res!r}")
    if not _control_active(dbf):
        raise DefectStillPresent(
            "after a failed handoff the system must stay QUIESCED (maintenance active), not resume"
        )

    # (G1) cleanup: bounded, atomic, deterministic; preserves young / NULL-age / every nonterminal row.
    dbg = base / "r20_cleanup.db"
    coord = _coordinator(client, dbg)
    for opk, te, st in (
        ("c10", 10.0, "FINAL"),
        ("c20", 20.0, "FINAL"),
        ("c30", 30.0, "FINAL"),
        ("cyoung", 1000.0, "FINAL"),
        ("cnull", None, "FINAL"),
    ):
        _seed_outbox_row(
            dbg, opk=opk, oid=f"obj-{opk:0<22}"[:26], collection=coll, state=st, terminal_epoch=te
        )
    # a nonterminal row that (contrived) carries a terminal_epoch, so ONLY the state filter protects it.
    _seed_outbox_row(
        dbg,
        opk="clive",
        oid="obj-clive0000000000000000000",
        collection=coll,
        state="PENDING",
        terminal_epoch=5.0,
    )
    rep = coord.cleanup_terminal(cutoff_epoch=100.0, batch_limit=2)
    survivors = set(_outbox_states(dbg))
    for must_live in ("clive", "cyoung", "cnull", "c30"):
        if must_live not in survivors:
            raise DefectStillPresent(
                f"cleanup must PRESERVE {must_live} (nonterminal / young / NULL-age / past-the-batch)"
            )
    if getattr(rep, "deleted", None) != 2:
        raise DefectStillPresent(f"cleanup must delete EXACTLY the batch (2), got {rep!r}")
    if {"c10", "c20"} & survivors:
        raise DefectStillPresent("cleanup must delete the two OLDEST eligible terminal rows")
    if getattr(rep, "remaining_eligible", None) != 1:
        raise DefectStillPresent(f"cleanup must report 1 still-eligible row (c30), got {rep!r}")

    # (G2) deterministic tie-break: equal terminal_epoch -> operation_key decides, not scan order.
    dbt = base / "r20_tie.db"
    coord = _coordinator(client, dbt)
    for opk in ("zzz", "aaa", "mmm"):  # inserted so scan order (zzz) != operation_key order (aaa)
        _seed_outbox_row(
            dbt,
            opk=opk,
            oid=f"obj-tie-{opk}00000000000000000"[:26],
            collection=coll,
            state="FINAL",
            terminal_epoch=50.0,
        )
    coord.cleanup_terminal(cutoff_epoch=100.0, batch_limit=1)
    if "aaa" in set(_outbox_states(dbt)):
        raise DefectStillPresent(
            "a terminal_epoch tie must break on operation_key (delete 'aaa'), not on scan/insertion order"
        )

    # (H) config validation (correct for every candidate; the boundary must reject junk).
    dbh = base / "r20_config.db"
    coord = _coordinator(client, dbh)
    for bad_cut in (0, -1.0, float("inf"), float("nan"), "x", True):
        try:
            coord.cleanup_terminal(cutoff_epoch=bad_cut, batch_limit=5)
        except _CleanupConfigError:
            pass
        else:
            raise DefectStillPresent(f"cleanup must reject cutoff_epoch={bad_cut!r}")
    for bad_batch in (0, -1, 2.5, "x", True):
        try:
            coord.cleanup_terminal(cutoff_epoch=100.0, batch_limit=bad_batch)
        except _CleanupConfigError:
            pass
        else:
            raise DefectStillPresent(f"cleanup must reject batch_limit={bad_batch!r}")

    # (I) migration backfill: stamp terminal_epoch from a known age; unknown age stays NULL (preserved).
    dbi = base / "r20_backfill.db"
    coord = _coordinator(client, dbi)
    _seed_outbox_row(
        dbi,
        opk="b1",
        oid="obj-b10000000000000000000000",
        collection=coll,
        state="FINAL",
        terminal_epoch=None,
        updated_epoch=42.0,
    )
    _seed_outbox_row(
        dbi,
        opk="b2",
        oid="obj-b20000000000000000000000",
        collection=coll,
        state="FINAL",
        terminal_epoch=None,
        updated_epoch=None,
    )
    if coord.backfill_terminal_epoch() != 1:
        raise DefectStillPresent("backfill must stamp exactly the terminal rows whose age is known")
    if _outbox_field(dbi, "b1", "terminal_epoch") != 42.0:
        raise DefectStillPresent("backfill must set terminal_epoch from the row's updated_epoch")
    if _outbox_field(dbi, "b2", "terminal_epoch") is not None:
        raise DefectStillPresent("a terminal row with no known age must stay NULL (preserved)")

    # (J) count->drop atomicity: a racing admission in a non-atomic count->drop gap must NOT be silently
    # destroyed. The CORRECT single txn holds the write lock across the count AND the drop, so a racing
    # insert is BLOCKED (never commits); check_then_drop_without_single_txn opens a lockless gap where the
    # insert commits and is then dropped.
    dbj = base / "r20_atomicdrop.db"
    coord = _coordinator(client, dbj)
    _seed_outbox_row(
        dbj,
        opk="t1",
        oid="obj-J1000000000000000000000",
        collection=coll,
        state="FINAL",
        terminal_epoch=1.0,
    )
    inj: dict[str, bool] = {"ok": False}

    def _inject(name: str) -> None:
        if name != "rollback_before_drop":
            return
        try:
            c2 = sqlite3.connect(
                str(dbj), timeout=0
            )  # timeout=0: fail fast if the row lock is held
            c2.execute(
                "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
                "expected_version,patch_sha,patch_json,intent_digest,state,event_id) VALUES "
                "('race','obj-race00000000000000000000',?,'matured',1,'s','{}','d','PENDING','ev')",
                (coll,),
            )
            c2.commit()
            c2.close()
            inj["ok"] = True
        except sqlite3.OperationalError:
            inj["ok"] = False

    coord._checkpoint = _inject
    coord.rollback(expected_generation=_control_generation(dbj) + 1)
    if inj["ok"] and not (_table_exists(dbj, "lifecycle_outbox") and "race" in _outbox_states(dbj)):
        raise DefectStillPresent(
            "a count->drop gap let a racing admission commit and then destroyed it (non-atomic rollback)"
        )


# ---- R20: TWO-PROCESS DRAIN PROOF via event-rendezvoused bounded polling (the key novelty) --------- #
#
# A real in-flight reader (child B) enters its barrier-aware critical section holding LOCK_SH and PARKS at
# a file barrier (writes B.inside, waits for `go`) - it does not sleep-to-hope, it waits for a definite
# rendezvous. The parent proves NON-OVERLAP with a LOCK_EX|LOCK_NB PROBE: while B holds
# LOCK_SH, fcntl.flock(LOCK_EX|LOCK_NB) MUST raise BlockingIOError (exclusive unavailable). A concurrent
# rollback (child C) is then required to DRAIN B: it must reach its post-lock point ("ex_acquired") only
# AFTER B has left (B.inside removed at LOCK_SH release). old_binary_ignores_lock lets the parent probe
# SUCCEED (B never took LOCK_SH); ack_without_drain / in_flight_old_generation / replaced_lock_inode reach
# "ex_acquired" while B is still inside (B.inside present -> C.leaked); check_before_quiesce drains but
# drops on a STALE presample -> destroys B's committed intent. Each discriminator is a durable file/lock
# fact reached by event-rendezvoused bounded polling (with a bounded negative-progress window), stable
# across 5x - not a fixed timing guess.

_R20_DRAIN_OBJECT = "r20drain00000000000000000000"


def _r20_reader_child_source(*, mode: str, db_path: Path, coll: str, barrier_dir: Path) -> str:
    """Child B: a barrier-aware admission that holds LOCK_SH for the whole transition. Its Qdrant apply is
    forced transient (stays PENDING). It marks B.inside on entry to the critical section, PARKS until the
    parent writes `go`, commits its durable intent, and clears B.inside just before releasing LOCK_SH."""
    return (
        "import os, time, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from tests.lifecycle.test_c6b_atomicity import "
        "_RefCoordinator as _Coord, _RefIntent as _Intent, _TransientQdrantError\n"
        f"_bd = {str(barrier_dir)!r}\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    _client = QdrantClient(':memory:')\n"
        "def _boom(*a, **k):\n"
        "    raise _TransientQdrantError('hold pending')\n"
        "_client.set_payload = _boom\n"
        f"_c = _Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})\n"
        "def _cp(name):\n"
        "    if name == 'before_pending_commit':\n"
        "        open(os.path.join(_bd, 'B.inside'), 'w').close()\n"
        f"        for _ in range(int({_RACE_BARRIER_TIMEOUT!r} / 0.01)):\n"
        "            if os.path.exists(os.path.join(_bd, 'go')): break\n"
        "            time.sleep(0.01)\n"
        "    if name == 'before_shared_release':\n"
        "        try:\n"
        "            os.unlink(os.path.join(_bd, 'B.inside'))\n"
        "        except FileNotFoundError:\n"
        "            pass\n"
        "_c._checkpoint = _cp\n"
        f"_c.transition(_Intent(collection={coll!r}, object_id={_R20_DRAIN_OBJECT!r}, "
        f"namespace={_NS!r}, expected_version=1, target_state='matured', actor='t', reason='r', "
        "operation_key='op-drainB'))\n"
        "os._exit(0)\n"
    )


def _r20_rollback_child_source(*, mode: str, db_path: Path, barrier_dir: Path) -> str:
    """Child C: a rollback that MUST drain B. At its post-lock point it flags C.leaked iff an in-flight
    reader (B.inside) is still inside - i.e. it took/represented an exclusive barrier without draining."""
    return (
        "import os, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from tests.lifecycle.test_c6b_atomicity import "
        "_RefCoordinator as _Coord, _control_generation\n"
        f"_bd = {str(barrier_dir)!r}\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    _client = QdrantClient(':memory:')\n"
        f"_c = _Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})\n"
        "def _cp(name):\n"
        "    if name == 'rollback_pre_lock':\n"
        "        open(os.path.join(_bd, 'C.quiesced'), 'w').close()\n"
        "    if name == 'ex_acquired' and os.path.exists(os.path.join(_bd, 'B.inside')):\n"
        "        open(os.path.join(_bd, 'C.leaked'), 'w').close()\n"
        "_c._checkpoint = _cp\n"
        f"_gen = _control_generation({str(db_path)!r})\n"
        "_c.rollback(expected_generation=_gen + 1)\n"
        "os._exit(0)\n"
    )


def _r20_reconciler_child_source(*, mode: str, db_path: Path, barrier_dir: Path) -> str:
    """Child B (RECONCILER role): a barrier-aware reconcile_once that holds LOCK_SH for the whole pass. It
    marks B.inside on entry to the critical section (its own wrapper, via role='reconcile'), PARKS until
    the parent writes `go`, then finishes, clearing B.inside just before releasing LOCK_SH. A distinct
    proof from admission: reconcile_once has its OWN barrier wrapper/role/selection - reconcile_bypasses_
    barrier bypasses LOCK_SH for the reconciler ONLY, so it is invisible to the admission proof."""
    return (
        "import os, time, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord\n"
        f"_bd = {str(barrier_dir)!r}\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    _client = QdrantClient(':memory:')\n"
        f"_c = _Coord(client=_client, db_path={str(db_path)!r}, mode={mode!r})\n"
        "def _cp(name):\n"
        "    if name == 'reconcile_entered':\n"
        "        open(os.path.join(_bd, 'B.inside'), 'w').close()\n"
        f"        for _ in range(int({_RACE_BARRIER_TIMEOUT!r} / 0.01)):\n"
        "            if os.path.exists(os.path.join(_bd, 'go')): break\n"
        "            time.sleep(0.01)\n"
        "    if name == 'before_shared_release':\n"
        "        try:\n"
        "            os.unlink(os.path.join(_bd, 'B.inside'))\n"
        "        except FileNotFoundError:\n"
        "            pass\n"
        "_c._checkpoint = _cp\n"
        # a non-draining WRONG rollback may have DROPPED the (empty) outbox while this reconciler was
        # parked; the post-`go` pass then finds no table. That is exactly the overlap the proof already
        # flagged (probe/leak), so swallow it and exit cleanly rather than emit a scary child traceback.
        "try:\n"
        "    _c.reconcile_once()\n"
        "except Exception:\n"
        "    pass\n"
        "os._exit(0)\n"
    )


def _r20_drain_core(base: Path, *, reader_src: str, role_label: str) -> tuple[Path, bool, bool]:
    """Shared two-process drain harness: spawn an in-flight barrier-aware reader (child B, given as source)
    that PARKS holding LOCK_SH; prove exclusive is unavailable with a LOCK_EX|LOCK_NB probe; run a
    concurrent rollback (child C) that must DRAIN B; release B; return (db_path, probe_free, leaked).
    Every wait is event-rendezvoused bounded polling (with a bounded negative-progress window), stable
    across 5x - not a fixed timing guess."""
    _api()  # xfail today (coordinator absent)
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "lifecycle.db"
    bdir = base / "barrier"
    bdir.mkdir(parents=True, exist_ok=True)
    maintlock = str(db_path) + _MAINTLOCK_SUFFIX
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _RefCoordinator(client=QdrantClient(":memory:"), db_path=db_path, mode=mode)  # schema

    b = subprocess.Popen([sys.executable, "-c", reader_src])
    # rendezvous: wait until B has entered its LOCK_SH critical section and PARKED (a definite condition).
    inside = bdir / "B.inside"
    deadline = time.time() + _RACE_BARRIER_TIMEOUT
    while not inside.exists():
        if b.poll() is not None or time.time() > deadline:
            raise DefectStillPresent(
                f"the in-flight {role_label} never reached its LOCK_SH critical section"
            )
        time.sleep(0.01)

    # THE drain proof: exclusive must be UNAVAILABLE while B holds LOCK_SH.
    pfd = os.open(maintlock, os.O_CREAT | os.O_RDWR, 0o600)
    probe_free = False
    try:
        fcntl.flock(pfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        probe_free = True  # acquired EX while B is inside -> the barrier did not hold
    except BlockingIOError:
        probe_free = False
    finally:
        os.close(pfd)

    # a concurrent rollback must DRAIN B before it acts. B stays parked (holding LOCK_SH, B.inside present)
    # until the parent releases it, so a NON-draining rollback reaches its post-lock point WHILE B is
    # inside (-> C.leaked) BEFORE B is ever released. A CORRECT (blocking-LOCK_EX) rollback cannot reach
    # that point until B releases, so it never leaks; it makes NO progress within the bounded window - a
    # durable file proves a leak, its absence a drained blocking-lock.
    leaked = bdir / "C.leaked"
    quiesced = bdir / "C.quiesced"
    c = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _r20_rollback_child_source(mode=mode, db_path=db_path, barrier_dir=bdir),
        ]
    )
    # wait until C has imported + durably quiesced and is at its lock boundary (a definite rendezvous). The
    # module import in the child dominates and is what the generous barrier timeout covers.
    qdeadline = time.time() + _RACE_BARRIER_TIMEOUT
    while not quiesced.exists() and c.poll() is None and time.time() < qdeadline:
        time.sleep(0.01)
    # from the lock boundary the discriminator is a handful of syscalls: a non-draining rollback reaches
    # its post-lock point (C.leaked) essentially immediately; a correct blocking-LOCK_EX rollback makes NO
    # progress until B releases. A short bounded negative-progress window cleanly separates the two.
    ldeadline = time.time() + 3.0
    while not leaked.exists() and c.poll() is None and time.time() < ldeadline:
        time.sleep(0.005)
    (bdir / "go").write_text("1")  # release B: finish its critical section, then release LOCK_SH
    b.wait(timeout=90)
    c.wait(timeout=90)
    return db_path, probe_free, leaked.exists()


def _check_r20_drain(base: Path) -> None:
    """ADMISSION-role drain proof (Yua R20 key novelty): a rollback's exclusive barrier must NOT overlap an
    in-flight barrier-aware admission's LOCK_SH critical section, and must not destroy its committing
    intent. A LOCK_EX|LOCK_NB probe must raise BlockingIOError while B is inside; a leak flag catches a
    rollback that reaches its post-lock point while B is inside; B's committed intent must survive."""
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    coll = str(collection_for_plane("episodic"))
    src = _r20_reader_child_source(
        mode=mode, db_path=base / "lifecycle.db", coll=coll, barrier_dir=base / "barrier"
    )
    db_path, probe_free, leaked = _r20_drain_core(base, reader_src=src, role_label="admission")
    if probe_free:
        raise DefectStillPresent(
            "LOCK_EX was acquired while an in-flight LOCK_SH admission was inside its critical section - "
            "the barrier did not hold (a reader that never took the shared lease is not drainable)"
        )
    if leaked:
        raise DefectStillPresent(
            "the rollback reached its post-lock disposition WHILE an in-flight admission was still inside "
            "- it did not drain the shared holder (ack_without_drain / in_flight_old_generation / "
            "replaced_lock_inode)"
        )
    if not _table_exists(db_path, "lifecycle_outbox"):
        raise DefectStillPresent(
            "a rollback dropped the outbox while a live admission's intent was committing (data loss)"
        )
    rows = _outbox_for_object(db_path, _R20_DRAIN_OBJECT)
    if not any(r["state"] in ("PENDING", "APPLIED") for r in rows):
        raise DefectStillPresent(
            "the in-flight admission's durable intent was destroyed by a racing rollback (data loss)"
        )


def _check_r20_reconciler_drain(base: Path) -> None:
    """RECONCILER-role drain proof (Yua R20 correction): reconcile_once has its OWN barrier wrapper and
    role selection, so it is proven SEPARATELY - the shared _barrier_admit code does not substitute. An
    in-flight reconciler holds LOCK_SH inside its pass; a rollback's exclusive barrier must not overlap it
    (LOCK_EX|LOCK_NB probe raises BlockingIOError; no post-lock overlap). reconcile_bypasses_barrier makes
    ONLY the reconciler skip LOCK_SH+the recheck - caught HERE (probe succeeds), invisible to admission."""
    mode = _ACTIVE_CANDIDATE._mode if _ACTIVE_CANDIDATE is not None else "correct"
    src = _r20_reconciler_child_source(
        mode=mode, db_path=base / "lifecycle.db", barrier_dir=base / "barrier"
    )
    _db_path, probe_free, leaked = _r20_drain_core(base, reader_src=src, role_label="reconciler")
    if probe_free:
        raise DefectStillPresent(
            "LOCK_EX was acquired while an in-flight LOCK_SH RECONCILER was inside its critical section - "
            "the reconciler did not hold the shared barrier (old_binary_ignores_lock / "
            "reconcile_bypasses_barrier)"
        )
    if leaked:
        raise DefectStillPresent(
            "the rollback reached its post-lock disposition WHILE an in-flight reconciler was still inside "
            "- it did not drain the shared holder (ack_without_drain / in_flight_old_generation / "
            "replaced_lock_inode)"
        )


_R20_REASON = (
    "today there is no rollback/maintenance barrier or terminal-row retention, so a schema rollback cannot "
    "refuse on a live intent, durably quiesce admission, hold its barrier through the deploy cutover, or "
    "bound/deterministically clean terminal rows; R20 needs rollback-refuses-nonterminal (drop nothing, "
    "stay quiesced - correction 4), a generation-fenced maintenance lifecycle, and an atomic age-bounded "
    "cleanup CTE that preserves young / NULL-age / every nonterminal row."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R20_REASON)
def test_r20_rollback_refuses_nonterminal_maintenance_lifecycle_and_cleanup(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r20(*env)


_R20_DRAIN_REASON = (
    "today there is no cross-process flock barrier, so a schema rollback cannot drain an in-flight "
    "ADMISSION; R20 needs a LOCK_SH admission barrier that an exclusive rollback drains - no LOCK_EX "
    "overlap while a shared holder is inside (proven by a LOCK_EX|LOCK_NB probe raising BlockingIOError) "
    "and no destruction of a committing intent - two real processes, via event-rendezvoused bounded "
    "polling (with a bounded negative-progress window), stable across 5x."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R20_DRAIN_REASON)
def test_r20_two_process_admission_drain_barrier_no_overlap(tmp_path: Path) -> None:
    _check_r20_drain(tmp_path)


_R20_RECONCILER_DRAIN_REASON = (
    "today there is no cross-process flock barrier, so a schema rollback cannot drain an in-flight "
    "RECONCILER; reconcile_once has its OWN barrier wrapper/role/selection, so it needs its own process "
    "drain proof - an exclusive rollback must not overlap an in-flight reconcile pass (LOCK_EX|LOCK_NB "
    "probe raising BlockingIOError; no post-lock overlap), with reconcile_bypasses_barrier caught here "
    "and NOWHERE in the admission proof - two real processes, via event-rendezvoused bounded polling "
    "(with a bounded negative-progress window), stable across 5x."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_R20_RECONCILER_DRAIN_REASON)
def test_r20_two_process_reconciler_drain_barrier_no_overlap(tmp_path: Path) -> None:
    _check_r20_reconciler_drain(tmp_path)


# ---- committed, RERUNNABLE red-proof harness (Yua evidence rule) ----------------------------------- #

_RED_PROOF: dict[str, tuple[Any, list[str]]] = {
    # red -> (check, plausible-wrong modes that MUST fail this check)
    "r1": (_check_r1, ["mutate_first", "premature_final"]),
    "r2": (_check_r2, ["mutate_first", "no_begin_catch"]),
    "r3": (_check_r3, ["classify_all_terminal", "reconcile_no_apply", "reconcile_greedy"]),
    "r4": (_check_r4, ["classify_all_transient"]),
    "r8": (_check_r8, ["finalize_not_atomic"]),
    "r9": (
        _check_r9,
        ["reconcile_no_readback", "finalize_dup_event_on_replay", "finalize_rekey_on_replay"],
    ),
    "r13": (
        _check_r13,
        [
            "readback_none",
            "readback_version_only",
            "readback_version_state_no_sha",
            "readback_hash_intended",
            "never_apply",  # hole 1: reports partial without landing state+version
            "duplicate_apply_marker",  # hole 3: writes a second effective-apply marker
        ],
    ),
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
    "r16": (
        _check_r16,
        [
            "claim_without_due_filter",
            "ignore_unexpired_owner",
            "shared_static_owner_token",
            "nonowner_release",
            "finalize_without_owner_guard",
            "release_in_separate_txn",
            "ttl_zero",
            "ttl_unbounded",
            "report_claimed_as_selected",
        ],
    ),
    "r17": (
        _check_r17,
        [
            "reclaim_before_expiry",
            "strict_lt_equality_bug",
            "never_reclaim",
            "reuse_owner_ABA",
            "stale_owner_finalizes",
            "stale_owner_effective_apply",
        ],
    ),
    "r18": (
        _check_r18,
        [
            "head_of_line_break",
            "fixed_first_row_reselection",
            "false_success_dropping",
        ],
    ),
    "r19": (
        _check_r19,
        [
            "full_payload_storage",
            "noncanonical_serialization",
            "log_interpolation",
            "raw_exception_reason",
            "object_namespace_labels",
            "high_cardinality_unknown_class",
            "empty_patch",
            "missing_required_patch",
            "omit_metrics",
            "double_count_metrics",
            "dynamic_metric_name",
        ],
    ),
    "r15": (
        _check_r15,
        [
            "abandon_after_n_attempts",
            "terminal_on_attempt_cap",
            "unknown_is_terminal",
            "attempts_not_tracked",
            "unbounded_backoff",
            "backoff_ignored",
            "fixed_clock_default",
            "scan_increments_attempts",
            "early_skip_increments_attempts",
            "non_atomic_attempt_schedule",
            "schedule_written_before_attempts",
            "lease_claim_not_due",
            "constant_backoff",
            "wrong_exponent_origin",
            "exponent_overflow_at_huge_attempts",
        ],
    ),
    "r14": (
        _check_r14,
        [
            "no_cap",  # never gates -> admits over the cap
            "off_by_one",  # '>' admits AT the cap
            "cap_after_qdrant",  # gates only after mutating Qdrant -> Qdrant touched
            "pending_only",  # ignores APPLIED -> undercounts the backlog
            "terminal_counts",  # counts terminal rows -> falsely rejects under the cap
            "cap_before_retry",  # gates before idempotency -> falsely rejects a same-key retry
        ],
    ),
    "r20": (
        _check_r20,
        [
            "rollback_ignores_nonterminal",  # drops despite a live PENDING/APPLIED intent (data loss)
            "clears_maintenance_on_refuse",  # a refused rollback must stay quiesced, not resume
            "local_flag_only",  # maintenance in-memory, not the durable control row
            "starving_new_readers",  # proceeds under LOCK_SH when active instead of refusing
            "worker_stopped_but_admission_live",  # only reconcilers quiesced; admission stays live
            "stale_quiescence_generation",  # accepts a generation != expected
            "check_active_before_shared_only",  # rechecks before the shared lock -> a slip-through race
            "release_barrier_before_deploy",  # releases EX before the handoff -> admits before cutover
            "releases_on_handoff_failure",  # resumes on handoff failure instead of staying quiesced
            "delete_nonterminal",  # cleanup drops a nonterminal row (data loss)
            "cleanup_deletes_young_terminal",  # deletes terminal rows at/after the cutoff (too young)
            "cleanup_unbounded_batch",  # ignores batch_limit
            "no_cleanup",  # a no-op cleanup
            "null_terminal_epoch",  # sweeps NULL-age terminal rows instead of preserving them
            "delete_outside_selected_batch",  # DELETE scoped past the selected batch (batch mismatch)
            "outer_and_inner_predicates_missing",  # eligibility predicate absent from inner AND outer
            "nondeterministic_tie",  # ORDER BY terminal_epoch only, no operation_key tiebreak
            "check_then_drop_without_single_txn",  # count and drop in separate txns -> a racing loss
        ],
    ),
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
    "r14race": (_check_r14_race, ["check_then_insert_race"]),
    "r16race": (_check_r16_race, ["select_then_update", "lease_overshoot_ttl"]),
    "r17reclaim": (_check_r17_reclaim, ["reclaim_reapplies_after_readback"]),
    "r22": (_check_r22, ["non_atomic_cas"]),
    "r20drain": (
        _check_r20_drain,  # ADMISSION-role process proof
        [
            "old_binary_ignores_lock",  # never takes LOCK_SH (both roles) -> probe acquires EX while inside
            "ack_without_drain",  # LOCK_EX|NB -> reaches disposition while an in-flight holder is inside
            "in_flight_old_generation",  # does not wait out an old-generation in-flight holder
            "replaced_lock_inode",  # swaps the lock inode -> no exclusion vs old-inode holders
            "check_before_quiesce",  # samples the backlog before EX -> drops a drained-window commit
        ],
    ),
    "r20reconciler": (
        _check_r20_reconciler_drain,  # RECONCILER-role process proof (non-redundant with admission)
        [
            "old_binary_ignores_lock",  # never takes LOCK_SH (both roles) -> probe succeeds while inside
            "reconcile_bypasses_barrier",  # role-specific: ONLY reconcile skips LOCK_SH -> caught ONLY here
            "ack_without_drain",  # LOCK_EX|NB -> overlaps the in-flight reconciler
            "in_flight_old_generation",  # does not wait out the in-flight reconciler
            "replaced_lock_inode",  # swaps the lock inode -> no exclusion vs the reconciler's old inode
        ],
    ),
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
