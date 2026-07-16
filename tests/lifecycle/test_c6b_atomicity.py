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
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time
import warnings
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import jwt
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, SecretStr, ValidationError, field_validator
from qdrant_client import QdrantClient, models

from musubi.api.app import create_app
from musubi.api.dependencies import (
    get_artifact_plane,
    get_concept_plane,
    get_curated_plane,
    get_embedder,
    get_episodic_plane,
    get_lifecycle_service,
    get_qdrant_client,
    get_reranker,
    get_settings_dep,
)
from musubi.embedding import FakeEmbedder
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.maturation import (
    MaturationConfig,
    MaturationCursor,
    OllamaClient,
    OllamaImportance,
    OllamaTopic,
    concept_demotion_sweep,
    concept_maturation_sweep,
    episodic_demotion_sweep,
    episodic_maturation_sweep,
    provisional_ttl_sweep,
)
from musubi.lifecycle.transitions import TransitionError
from musubi.observability.registry import default_registry, render_text_format
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.concept import ConceptPlane
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.types.artifact import SourceArtifact
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge
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
_PRESENT_TRANSITION_BYPASSES: set[tuple[str, str]] = set()


# G1 - CLOSURE-GATE (not Phase-1 acceptance): no direct state mutation outside the coordinator --------- #


def test_g1_no_direct_state_transition_setpayload_outside_coordinator() -> None:
    """Closure-gate (NOT Phase-1 acceptance): C6b atomicity is not closed until EVERY state-writing
    transition `set_payload` routes through the coordinator."""
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
    return cur


def _is_coordinator_annotation(ann: ast.AST | None) -> bool:
    """A BARE coordinator type annotation - `LifecycleTransitionCoordinator` or `module.LifecycleTransition
    Coordinator`. An Optional/Union form (`X | None`, `Optional[X]`, `Union[...]`) is NOT bare and does NOT
    match, so an optional injected coordinator fails closed (Yua: no optional unaudited fallback)."""
    return (isinstance(ann, ast.Name) and ann.id == _COORDINATOR_CLASS) or (
        isinstance(ann, ast.Attribute) and ann.attr == _COORDINATOR_CLASS
    )


def _params_with_defaults(args: ast.arguments) -> list[tuple[ast.arg, bool]]:
    """Every parameter of a signature paired with whether it has a default (positional defaults align to the
    tail; a kw-only default is a non-None `kw_defaults` slot)."""
    positional = [*args.posonlyargs, *args.args]
    n_def = len(args.defaults)
    out: list[tuple[ast.arg, bool]] = [
        (a, i >= len(positional) - n_def) for i, a in enumerate(positional)
    ]
    out.extend((a, d is not None) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True))
    return out


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
    is rebound to something else - the map alone no longer implies coordinator provenance.
    INJECTED BOUNDARY (Yua): a REQUIRED, BARE-typed `coordinator: LifecycleTransitionCoordinator` parameter
    is seeded as a coordinator binding at function entry (so a call in the body resolves), while an
    optional/defaulted/Union-typed param is NOT seeded (fails closed), and a later body rebinding of that
    name adds a second binding -> ambiguous -> fails closed."""
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
    # injected boundary: a required, bare-typed coordinator parameter is a coordinator binding at entry
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg, has_default in _params_with_defaults(fn.args):
                if not has_default and _is_coordinator_annotation(arg.annotation):
                    func_binds.setdefault(fn, {}).setdefault(arg.arg, []).append((arg.lineno, True))
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
    "transition wrapper of lifecycle/transitions.py, on a REQUIRED injected `coordinator: "
    "LifecycleTransitionCoordinator` parameter) does not yet exist - the Phase-1 coordinator is unbuilt, so "
    "the resolved inventory is empty and cannot equal the reviewed set. Flips to XPASS(strict) when the "
    "coordinator lands and the single reviewed callsite is present on the injected boundary "
    "(zero/missing/duplicate/extra, and an optional/defaulted/rebound/unrelated receiver, all fail). H5 "
    "extends the reviewed set as it migrates other state-mutating paths."
)


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

    # ---- INJECTED BOUNDARY (Yua): a REQUIRED, bare-typed coordinator PARAMETER is the reviewed seam ----
    # a required injected `coordinator: LifecycleTransitionCoordinator` param resolves the body call
    injected_ok = ast.parse(
        "def transition(client, coordinator: LifecycleTransitionCoordinator):\n"
        "    return coordinator.transition(i)\n"
    )
    assert sorted(_resolve_callsites(injected_ok, "m")) == reviewed
    # a module-qualified annotation is also recognized
    injected_qual = ast.parse(
        "def transition(client, coordinator: lifecycle.LifecycleTransitionCoordinator):\n"
        "    return coordinator.transition(i)\n"
    )
    assert sorted(_resolve_callsites(injected_qual, "m")) == reviewed
    # FAIL-CLOSED: a free name with NO injected coordinator param does not count
    unbound_free = ast.parse("def transition(client):\n    return coordinator.transition(i)\n")
    assert _resolve_callsites(unbound_free, "m") == []
    # FAIL-CLOSED: an OPTIONAL/Union-typed injected coordinator (a fallback) does not count
    optional_union = ast.parse(
        "def transition(client, coordinator: LifecycleTransitionCoordinator | None):\n"
        "    return coordinator.transition(i)\n"
    )
    assert _resolve_callsites(optional_union, "m") == []
    # FAIL-CLOSED: a DEFAULTED injected coordinator (optional fallback) does not count
    defaulted = ast.parse(
        "def transition(client, coordinator: LifecycleTransitionCoordinator = FALLBACK):\n"
        "    return coordinator.transition(i)\n"
    )
    assert _resolve_callsites(defaulted, "m") == []
    # FAIL-CLOSED: an injected param REBOUND in the body is ambiguous
    injected_rebound = ast.parse(
        "def transition(client, coordinator: LifecycleTransitionCoordinator):\n"
        "    coordinator = make_plane()\n"
        "    return coordinator.transition(i)\n"
    )
    assert _resolve_callsites(injected_rebound, "m") == []
    # FAIL-CLOSED: an UNRELATED-typed param whose .transition is called does not count
    unrelated_param = ast.parse(
        "def transition(client, plane: SomethingElse):\n    return plane.transition(i)\n"
    )
    assert _resolve_callsites(unrelated_param, "m") == []


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


def _is_outbox_ref(tok: _Tok) -> bool:
    """The lifecycle_outbox table reference, bare or as a quoted identifier of the same value."""
    return tok in (("WORD", "LIFECYCLE_OUTBOX"), ("QID", "LIFECYCLE_OUTBOX"))


def _starts_select_from(body: list[_Tok]) -> bool:
    """The inner CTE body BEGINS with exactly `SELECT operation_key FROM lifecycle_outbox`."""
    return (
        len(body) >= 4
        and body[0] == ("WORD", "SELECT")
        and body[1] == ("WORD", "OPERATION_KEY")
        and body[2] == ("WORD", "FROM")
        and _is_outbox_ref(body[3])
    )


def _starts_delete_from(body: list[_Tok]) -> bool:
    """The outer statement BEGINS with exactly `DELETE FROM lifecycle_outbox`."""
    return (
        len(body) >= 3
        and body[0] == ("WORD", "DELETE")
        and body[1] == ("WORD", "FROM")
        and _is_outbox_ref(body[2])
    )


def _followed_by_param(body: list[_Tok], head: list[_Tok]) -> bool:
    """`head` (an exact token run) occurs immediately followed by a bound-parameter token - so `LIMIT :batch`
    and `terminal_epoch < :cutoff` pass while `LIMIT NULL` / `terminal_epoch << :cutoff` (the operand is not
    a single-`<` parameter) fail closed."""
    m = len(head)
    return any(body[i : i + m] == head and body[i + m][0] == "PARAM" for i in range(len(body) - m))


def _cleanup_sql_violations(sql_args: list[str]) -> list[str]:
    """Bounded-PARSER shape violations of the cleanup DELETE (a real tokenizer + token-SEQUENCE matching,
    NOT substring matching and NOT a full SQL parser; fails closed when the supported shape cannot be
    recognized unambiguously). [] = the pinned atomic shape: ONE executable statement whose parentheses are
    balanced; a `WITH <sel>` CTE whose inner body BEGINS `SELECT operation_key FROM lifecycle_outbox` and
    whose tiebreak ends `ORDER BY terminal_epoch, operation_key LIMIT <param>` (bounded LIMIT immediately
    after the tiebreak); an outer statement that BEGINS `DELETE FROM lifecycle_outbox`, is restricted to
    `operation_key IN (SELECT operation_key FROM <sel>)`, and ENDS at the terminal `RETURNING operation_key`
    with nothing after; the FULL terminal-eligibility predicate - state IN ('FINAL','ABANDONED') AND
    terminal_epoch IS NOT NULL AND terminal_epoch < <param> - as exact token runs in BOTH halves. Comments,
    string contents, and quoted identifiers never satisfy a keyword; a non-terminal string literal, an
    unrelated table, a mis-ordered/unbounded LIMIT, a non-terminal RETURNING, or unbalanced parens each fail
    closed. Count SELECTs are ignored."""
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
    if not _starts_select_from(inner):
        v.append(
            "inner_not_select"
        )  # inner prefix must be SELECT operation_key FROM lifecycle_outbox
    if not _starts_delete_from(outer):
        v.append("outer_not_delete")  # outer prefix must be DELETE FROM lifecycle_outbox
    ret = [("WORD", "RETURNING"), ("WORD", "OPERATION_KEY")]
    if not _has_seq(outer, ret):
        v.append("missing_returning")  # RETURNING must return operation_key, not a bare RETURNING
    elif outer[-2:] != ret:
        v.append(
            "returning_not_terminal"
        )  # nothing may follow the terminal RETURNING operation_key
    tie = [
        ("WORD", "ORDER"),
        ("WORD", "BY"),
        ("WORD", "TERMINAL_EPOCH"),
        ("PUNCT", ","),
        ("WORD", "OPERATION_KEY"),
    ]
    tie_present = _has_seq(inner, tie)
    limit_bound = _followed_by_param(inner, [("WORD", "LIMIT")])
    if not tie_present:
        v.append("nondeterministic_tie")  # ORDER BY missing the operation_key tiebreak
    if not limit_bound:
        v.append("missing_bound")  # LIMIT must be a bound parameter, not NULL/a literal
    if tie_present and limit_bound and not _followed_by_param(inner, [*tie, ("WORD", "LIMIT")]):
        v.append(
            "limit_not_after_order"
        )  # the bounded LIMIT must immediately follow the ORDER BY tiebreak
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
    for half, body in (("inner", inner), ("outer", outer)):
        if not _has_terminal_state(body):
            v.append(f"missing_{half}_state")
        if not _has_seq(body, notnull):
            v.append(f"missing_{half}_notnull")
        if not _followed_by_param(body, [("WORD", "TERMINAL_EPOCH"), ("PUNCT", "<")]):
            v.append(
                f"missing_{half}_age"
            )  # exact `terminal_epoch < <param>`, not `<<` or a literal
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
    "via a bounded tokenizer with real identifier boundaries + exact token-SEQUENCE and POSITION matching "
    "(comments, string contents, and quoted identifiers never satisfy a keyword; parentheses must balance; "
    "the table is pinned to lifecycle_outbox; the bounded LIMIT must follow the tiebreak; RETURNING "
    "operation_key must be terminal), not substring matching. Zero or multiple coordinator cleanup methods, "
    "a split select/delete, a trailing statement, unbalanced parens, an unrelated table, a "
    "mis-ordered/unbounded LIMIT, a non-terminal RETURNING, any missing inner/outer state/non-null/age "
    "component, a missing tiebreak/RETURNING, or a dynamic/unresolvable SQL argument each fail."
)


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

    # ---- token POSITION: a present keyword in the WRONG structural slot is not the contract shape ----
    # the outer statement must START with DELETE FROM, not SELECT DELETE / UPDATE DELETE
    assert "outer_not_delete" in _cleanup_sql_violations(
        [
            _G2B_CORRECT.replace(
                "DELETE FROM lifecycle_outbox", "SELECT DELETE FROM lifecycle_outbox"
            )
        ]
    )
    assert "outer_not_delete" in _cleanup_sql_violations(
        [
            _G2B_CORRECT.replace(
                ") DELETE FROM lifecycle_outbox", ") UPDATE DELETE FROM lifecycle_outbox"
            )
        ]
    )
    # the inner CTE body must START with SELECT operation_key FROM, not UPDATE ...
    assert "inner_not_select" in _cleanup_sql_violations(
        [
            _G2B_CORRECT.replace(
                "SELECT operation_key FROM lifecycle_outbox",
                "UPDATE operation_key FROM lifecycle_outbox",
            )
        ]
    )
    # RETURNING must return operation_key, not a bare RETURNING
    assert _cleanup_sql_violations(
        [_G2B_CORRECT.replace("RETURNING operation_key", "RETURNING")]
    ) == ["missing_returning"]
    # LIMIT must be a bound parameter, not NULL/a literal
    assert _cleanup_sql_violations([_G2B_CORRECT.replace("LIMIT :batch", "LIMIT NULL")]) == [
        "missing_bound"
    ]
    # the age comparator must be a single `<` with a bound parameter, not `<<` (both halves here)
    double_lt = _cleanup_sql_violations(
        [_G2B_CORRECT.replace("terminal_epoch < :cutoff", "terminal_epoch << :cutoff")]
    )
    assert "missing_inner_age" in double_lt and "missing_outer_age" in double_lt

    # ---- table + prefix/suffix EXACTNESS: a present-but-misplaced token is not the pinned shape ----
    # the pinned table is lifecycle_outbox; an unrelated table fails BOTH prefixes
    unrelated_table = _cleanup_sql_violations(
        [_G2B_CORRECT.replace("lifecycle_outbox", "unrelated_table")]
    )
    assert "inner_not_select" in unrelated_table and "outer_not_delete" in unrelated_table
    # the bounded LIMIT must IMMEDIATELY FOLLOW the ORDER BY tiebreak (both tokens present, wrong order)
    assert "limit_not_after_order" in _cleanup_sql_violations(
        [
            _G2B_CORRECT.replace(
                "ORDER BY terminal_epoch, operation_key LIMIT :batch",
                "LIMIT :batch ORDER BY terminal_epoch, operation_key",
            )
        ]
    )
    # nothing may follow the terminal RETURNING operation_key (tokens appended in the same statement)
    assert "returning_not_terminal" in _cleanup_sql_violations(
        [_G2B_CORRECT.replace("RETURNING operation_key", "RETURNING operation_key, note")]
    )

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
        if (
            self._mode != "omit_log"
        ):  # WRONG (omit_log): drop the required static failure log entirely
            _LIFECYCLE_LOGGER.warning("lifecycle_mutation_failed", extra={"c6b": extra})
        if self._mode == "omit_metrics":  # WRONG: emit NO metric (no observability at all)
            return
        metric_class = (
            "terminal" if cls == "terminal" else "transient"
        )  # unknown -> transient (bounded)
        if (
            self._mode == "unknown_class_terminal"
        ):  # WRONG: mislabel an unknown failure's external class as terminal (not transient)
            metric_class = "terminal"
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
        self.CleanupConfigError = _CleanupConfigError

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
        from musubi.lifecycle import coordinator as _c
    except ImportError as e:
        raise DefectStillPresent(
            "the Phase-1 coordinator module is not implemented (LifecycleTransitionCoordinator + "
            "TransitionIntent + TransitionFinal/TransitionPending)"
        ) from e
    return _c


def _real_lacks_stage(method: str) -> bool:
    """True when NO red-proof candidate is active AND the real src coordinator does not yet expose
    `method` (a later-slice capability, e.g. `reconcile_once`=S4, `rollback`=S6, `_apply_conditional`
    =S3). NO-OP (returns False) whenever `_ACTIVE_CANDIDATE` is set, so the wrong-candidate
    discriminator matrix — which always carries full capability — is never touched."""
    if _ACTIVE_CANDIDATE is not None:
        return False
    coord_cls = getattr(_api(), "LifecycleTransitionCoordinator", None)
    return coord_cls is None or not hasattr(coord_cls, method)


def _require_real_stage(method: str, reason: str) -> None:
    """Stage-capability guard for the frozen reds. When S2's admission-only real coordinator has not
    built a later-slice capability, raise the red's OWN intended DefectStillPresent — so a
    partially-built coordinator keeps each owed red strict-xfail, not a raw AttributeError/
    OperationalError that would break the strict-xfail contract. NO-OP under an active candidate."""
    if _real_lacks_stage(method):
        raise DefectStillPresent(reason)


# ---- fixtures + shared helpers --------------------------------------------------------------------- #


def _make_env(base: Path, namespace: str = _NS) -> tuple[QdrantClient, _Seed, Path]:
    # namespace defaults to _NS, preserving every existing caller behavior. R19 (S5) passes the PII
    # sentinel namespace so the REAL namespace-fenced coordinator finds/applies the actual seeded object.
    base.mkdir(parents=True, exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    plane = EpisodicPlane(client=client, embedder=FakeEmbedder())
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=namespace, content="c6b-seed")))
    seed = _Seed(
        collection=str(collection_for_plane("episodic")),
        object_id=str(obj.object_id),
        namespace=namespace,
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
    # count apply ATTEMPTS (delegates to the failing set_payload)
    calls = _count_set_payload(client)
    coord.transition(_intent(seed, to_state="matured", operation_key="op-r1"))
    if _ACTIVE_CANDIDATE is None and calls["n"] != 1:
        # Non-vacuity (Yua): R1 proves durable-intent-BEFORE-mutation only if the mutation was
        # actually ATTEMPTED. S2 admission stops at PENDING and attempts zero applies, so the claim
        # is not yet proven — R1 is owed to S3 conditional apply.
        raise DefectStillPresent(
            "R1 requires EXACTLY ONE attempted apply (the transient-faulted mutation) before it can "
            f"accept PENDING as durable-before-mutation; S2 admission attempted {calls['n']}, owed to S3"
        )
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


def test_r1_durable_intent_persisted_before_qdrant_mutation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r1(*env)


def test_r2_durable_begin_failure_blocks_qdrant_mutation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r2(*env)


def test_r3_transient_failure_is_ok_pending_then_reconciles(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r3(*env)


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


# R19 (S5) real-path helpers. The REAL coordinator emits ONLY through the process-wide default_registry
# (no `_metrics`/`_prometheus_exposition` test mirror, per Yua R-14:23), so the real path reads the shared
# exposition and asserts selected-series DELTAS (the counter is monotonic + process-wide) and absolute
# values (the gauge is a SET). A wrong candidate NEVER writes the default_registry (it is a test-local
# _RefCoordinator whose evidence stays on `_metrics`), so the real snapshot below is only exercised by the
# real coordinator and never polluted by the discrimination matrix (Yua R-14:28).
#: Standard LogRecord attributes plus the formatting artifacts a handler adds when it renders the record
#: (`message` = getMessage(), `asctime`) - none of which are coordinator-supplied PII/prohibited fields.
_R19_BASELINE_LOGRECORD_KEYS = frozenset(
    logging.LogRecord(
        name="x", level=logging.WARNING, pathname="p", lineno=1, msg="m", args=None, exc_info=None
    ).__dict__
) | {"message", "asctime"}


def _r19_series_value(text: str, name: str, labels: dict[str, str]) -> float:
    """The value of EXACTLY one Prometheus series (`name{sorted labels} value`) in an exposition text,
    or 0.0 if absent. Used to diff before/after snapshots of the shared registry for the real path."""
    if labels:
        target = name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "} "
    else:
        target = name + " "
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(target):
            return float(line[len(target) :].strip())
    return 0.0


def _check_r19(client: QdrantClient, seed: _Seed, db_path: Path) -> None:
    """PII-free content + bounded observability (Yua R19): the outbox persists ONLY the canonical minimal
    target patch (state/version/lineage) + matching SHA - no arbitrary payload/unknown keys; and the
    coordinator NEVER emits operation/object/namespace/content/patch/reason/token into logs or metric
    labels. Metrics are a no-label pending gauge + a failures counter whose only label is class in
    {terminal, transient} (a durable ``unknown`` maps to the external ``transient`` class - never an
    ``unknown`` label). Adversarial PII sentinels are injected into the operation_key, the intent
    namespace/reason, and the exception message, then checked across the row, the captured log, and the
    metric exposition. The REAL coordinator emits through the shared default_registry (snapshot
    before/after + selected-series delta), a wrong candidate through its test-local `_metrics` surface.
    Two non-vacuity holes are closed: the metric class of an unknown failure MUST be ``transient`` (+1,
    with no ``terminal``/``unknown`` pollution), and the static ``lifecycle_mutation_failed`` log MUST
    exist carrying failure_class=unknown and no PII/prohibited fields. R13 full-readback SHA and R15
    unknown classification (without leaking reason) are preserved."""
    real = _ACTIVE_CANDIDATE is None
    base = Path(db_path).parent
    # Yua R-14:34: `PIISENTINEL-ns` is INVALID under Namespace validation; use a VALID dedicated namespace
    # that still carries the sentinel, seed the REAL object there (via _make_env), and use the RETURNED
    # seed so the namespace-fenced coordinator finds/applies the actual row. BOTH the sentinel AND the
    # exact namespace string are prohibited leak needles.
    pii_namespace = f"{_PII_SENTINEL.lower()}-ns/presence/episodic"
    c, s, d = _make_env(base / "r19", namespace=pii_namespace)
    poison = s
    op = f"{_PII_SENTINEL}-op"
    reason = f"{_PII_SENTINEL}-reason"
    needles = (_PII_SENTINEL, pii_namespace)
    before = render_text_format(default_registry()) if real else ""
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
    if any(n in str(row["patch_json"]) for n in needles):
        raise DefectStillPresent("R19 no PII may appear in the persisted patch")

    # ROW (durable classification, real path): the injected unknown apply failure must be recorded
    # DURABLY on the outbox row as failure_class='unknown' - queried directly from the store DB, since
    # the external metric alone is insufficient (a candidate could emit a metric without persisting).
    if real:
        durable_class = _outbox_field(d, op, "failure_class")
        if durable_class != "unknown":
            raise DefectStillPresent(
                "R19 the outbox row must durably record failure_class='unknown' for the injected "
                f"unknown apply failure; got {durable_class!r}"
            )

    # LOG (PII): no captured record may carry any leak needle (message OR structured fields).
    for r in cap.records:
        blob = f"{r.getMessage()} {r.__dict__}"
        if any(n in blob for n in needles):
            raise DefectStillPresent(f"R19 no PII may appear in a log record: {blob[:120]}")

    # LOG (non-vacuity): the static failure event MUST exist carrying failure_class=unknown - a candidate
    # that omits the log cannot vacuously pass. The REAL coordinator carries a DIRECT failure_class
    # attribute and NO prohibited extra fields; a test-local reference nests it under `c6b`.
    if real:
        direct = [
            r
            for r in cap.records
            if r.getMessage() == "lifecycle_mutation_failed"
            and getattr(r, "failure_class", None) == "unknown"
        ]
        if len(direct) != 1:
            raise DefectStillPresent(
                "R19 exactly ONE 'lifecycle_mutation_failed' log with a DIRECT failure_class=unknown "
                f"attribute is required, got {len(direct)}"
            )
        extra_keys = set(direct[0].__dict__) - _R19_BASELINE_LOGRECORD_KEYS
        if extra_keys != {"failure_class"}:
            raise DefectStillPresent(
                "R19 the failure log must carry ONLY failure_class (no PII/prohibited fields); "
                f"unexpected extra fields: {sorted(extra_keys - {'failure_class'})}"
            )
        # LOG (no traceback): discriminate the no-traceback requirement EXPLICITLY - the record must carry
        # no exception/stack payload (which could embed the PII exc message or a traceback), not merely
        # infer it from the extra-key shape.
        rec = direct[0]
        if not (rec.exc_info is None and rec.exc_text is None and rec.stack_info is None):
            raise DefectStillPresent(
                "R19 the failure log must carry NO traceback: exc_info/exc_text/stack_info must all be "
                f"None; got exc_info={rec.exc_info!r} exc_text={rec.exc_text!r} stack_info={rec.stack_info!r}"
            )
    else:
        logged = [
            r
            for r in cap.records
            if r.getMessage() == "lifecycle_mutation_failed"
            and (getattr(r, "c6b", None) or {}).get("failure_class") == "unknown"
        ]
        if len(logged) != 1:
            raise DefectStillPresent(
                "R19 exactly ONE 'lifecycle_mutation_failed' log carrying failure_class=unknown is "
                f"required, got {len(logged)}"
            )

    # METRICS: emission is REQUIRED. The unknown failure must be exactly ONE external `class=transient`
    # delta (+1), NO `class=terminal` delta, and NO `unknown` class anywhere; the pending gauge is
    # exactly 1 and UNLABELED. Names are whitelisted + PII-free.
    if real:
        after = render_text_format(default_registry())
        fname = "musubi_lifecycle_outbox_mutation_failures_total"
        d_transient = _r19_series_value(after, fname, {"class": "transient"}) - _r19_series_value(
            before, fname, {"class": "transient"}
        )
        d_terminal = _r19_series_value(after, fname, {"class": "terminal"}) - _r19_series_value(
            before, fname, {"class": "terminal"}
        )
        if d_transient != 1.0:
            raise DefectStillPresent(
                f"R19 the unknown failure must map to EXACTLY one class=transient delta (+1), got {d_transient}"
            )
        if d_terminal != 0.0:
            raise DefectStillPresent(
                f"R19 no class=terminal failure delta is expected for an unknown failure, got {d_terminal}"
            )
        if 'class="unknown"' in after:
            raise DefectStillPresent("R19 no 'unknown' metric class may ever be emitted")
        if "musubi_lifecycle_outbox_pending{" in after:
            raise DefectStillPresent("R19 the pending gauge must be UNLABELED")
        pend = _r19_series_value(after, "musubi_lifecycle_outbox_pending", {})
        if pend != 1.0:
            raise DefectStillPresent(
                f"R19 the pending gauge must be exactly 1 (the poison row), got {pend}"
            )
        if any(n in after for n in needles):
            raise DefectStillPresent("R19 no PII may appear in the metric exposition")
    else:
        for name, labels, _value in coord._metrics:
            if name not in _ALLOWED_METRIC_NAMES or any(n in name for n in needles):
                raise DefectStillPresent(
                    f"R19 metric name must be in the whitelist {sorted(_ALLOWED_METRIC_NAMES)}, PII-free; got {name!r}"
                )
            if name == "musubi_lifecycle_outbox_pending":
                if labels:
                    raise DefectStillPresent(
                        f"R19 the pending gauge must be UNLABELED, got {labels}"
                    )
            elif set(labels.keys()) != {"class"}:
                raise DefectStillPresent(
                    f"R19 the failures metric must have EXACTLY a 'class' label (no object/namespace); "
                    f"got {set(labels.keys())}"
                )
            elif labels["class"] not in ("terminal", "transient"):
                raise DefectStillPresent(
                    f"R19 the metric class label must be bounded to terminal|transient; got {labels['class']!r}"
                )
            if any(any(n in str(v) for n in needles) for v in labels.values()):
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
            raise DefectStillPresent(
                f"R19 the failure counter delta must be 1, got {failures[0][2]}"
            )
        if failures[0][1].get("class") != "transient":
            raise DefectStillPresent(
                "R19 the unknown failure must map to the external class=transient (not terminal/unknown); "
                f"got {failures[0][1].get('class')!r}"
            )
        if any(n in coord._prometheus_exposition() for n in needles):
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
            to_state="demoted",
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
            to_state="demoted",
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
    "pre-LIFE-010 expected_version was warn-only (last writer wins), so a stale writer clobbered. "
    "LIFE-010 / H7 (Issue #556, slice slice-life010-transition-conflict) shipped the hard "
    "fence: stale expected_version now returns Err(version_fence_violation) BEFORE legality or "
    "coordinator.apply, and the coordinator never sees a no-op mutation. R12 still validates "
    "the historic red; the shipped contract is the green guarantee."
)


def test_r9_idempotent_replay(env: tuple[QdrantClient, _Seed, Path]) -> None:
    _check_r9(*env)


def test_r13_conditional_apply_full_readback_patch_sha(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r13(*env)


def test_r14_hard_pending_cap_admission_backpressure(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r14(*env)


def test_r15_transient_never_abandoned_by_attempt_count(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r15(*env)


def test_r16_valid_lease_exclusive_processing(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r16(*env)


def test_r17_expired_owner_reclaim_safe(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r17(*env)


def test_r18_no_poison_row_starvation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r18(*env)


def test_r19_pii_free_content_and_bounded_observability(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r19(*env)


def test_r10_operation_key_idempotent_across_caller_retries(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    _check_r10(*env)


def test_r11_single_active_intent_per_object(tmp_path: Path) -> None:
    _check_r11(tmp_path)


_R14_RACE_REASON = (
    "today admission is not atomic across processes, so two concurrent begins at cap-1 both count under "
    "the cap and both insert (a check-then-insert race) -> the backlog exceeds the cap; R14 needs BEGIN "
    "IMMEDIATE -> count -> insert -> commit in one transaction so exactly one admits and one is cap_exceeded."
)


def test_r14_two_process_admission_race_holds_cap(tmp_path: Path) -> None:
    _check_r14_race(tmp_path)


_R16_RACE_REASON = (
    "today a reconciliation claim is not atomic across processes, so two workers can both check a row as "
    "claimable and both take it (a check-then-update race) -> concurrent double-processing; R16 needs ONE "
    "committed guarded UPDATE where rowcount==1 is ownership, so exactly one worker claims and one loses."
)


def test_r16_two_process_claim_race_one_owner(tmp_path: Path) -> None:
    _check_r16_race(tmp_path)


_R17_RECLAIM_REASON = (
    "today a dead worker's lease is never reclaimed and a crash-applied mutation has no owner to finalize "
    "it; R17 needs a new owner to reclaim the expired lease, READBACK-CONFIRM the already-applied mutation, "
    "and finalize it exactly once WITHOUT a second effective apply - proven with a real process that dies "
    "over an on-disk Qdrant."
)


def test_r17_crash_reclaim_readback_confirms_no_reapply(tmp_path: Path) -> None:
    _check_r17_reclaim(tmp_path)


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


def test_r5_crash_after_pending_before_qdrant(tmp_path: Path) -> None:
    _check_r5(tmp_path)


def test_r6_crash_after_qdrant_before_applied(tmp_path: Path) -> None:
    _check_r6(tmp_path)


def test_r7_crash_after_applied_before_finalize(tmp_path: Path) -> None:
    _check_r7(tmp_path)


def test_r8_finalize_transaction_is_atomic(env: tuple[QdrantClient, _Seed, Path]) -> None:
    _check_r8(*env)


# ---- R22: two DIFFERENT transitions race on one object, TWO REAL PROCESSES (Yua R22 rebuild) ------- #
#
# Yua rejected the sequential version. This is a real race: two OS processes with DIFFERENT operation
# keys, DIFFERENT targets, the SAME expected_version, on ONE object, over a SHARED ON-DISK Qdrant + SQLite.
# It is DETERMINISTIC by an EXPLICIT cross-process rendezvous at the atomic apply boundary (Yua's
# flake-harden ruling; see _r22_child_source for the mechanism + the two-winner diagnosis it fixes), NOT by
# scheduler luck: the WINNER runs its whole transition and FREES the on-disk Qdrant lock before signalling;
# the LOSER blocks at before_pending_commit until then, so it inserts AFTER the winner's row is FINAL (its
# PENDING insert passes ux_active_intent) and issues a version-fenced conditional apply against a FREE
# Qdrant already at the winner's version -> a guaranteed ZERO-match -> version_fence_violation, so its
# readback shows the winner's target and it NEVER mutates. Each child exits with an op-DISTINCT winner code
# (a WIN requires an Ok that actually FINALIZED) so the parent correlates the winning op to the single FINAL
# row, its audit event to_state, the single effective-apply marker, and the exact Qdrant target/version - a
# loser that overwrote-then-abandoned while the other finalized cannot pass. Wrong candidate: non_atomic_cas
# ONLY (index off AND fence off -> the real lost update / two effective applies: the loser applies WITHOUT
# the version fence and finalizes a second winner). R11 owns the naive-check-then-insert (no_unique_index)
# proof; in R22 the index and the fence are belt-and-suspenders, so removing only the index is caught by the
# fence and is not an R22 discriminator - R22 discriminates on the FENCE at the apply boundary.

_R22_WIN_A = 31  # op-a22 (-> matured) won
_R22_WIN_B = 34  # op-b22 (-> demoted) won
_R22_CONFLICT = 32
_R22_FENCE = 33


def _r22_child_source(
    *,
    role: str,
    mode: str,
    db_path: Path,
    qdrant_path: Path,
    seed: _Seed,
    target: str,
    op_key: str,
    win_code: int,
    barrier_dir: Path,
) -> str:
    """A child of the R22 two-process apply-boundary proof, hardened to DETERMINISM by an EXPLICIT
    cross-process rendezvous at the claimed atomic boundary (Yua's flake-harden ruling — no timing,
    no scheduler-luck). The WINNER runs its whole transition (INSERT PENDING -> version-fenced apply
    -> APPLIED -> FINAL), then RELEASES the on-disk Qdrant single-writer lock (closes its client)
    and only THEN signals `winner_done`. The LOSER blocks at `before_pending_commit` until
    `winner_done`, so it ALWAYS inserts AFTER the winner's row is FINAL (its PENDING insert passes
    ux_active_intent, which fences only PENDING/APPLIED) and applies against a FREE Qdrant already at
    the winner's version -> its version-fenced conditional apply is a guaranteed ZERO-match ->
    version_fence_violation, EVERY run. This removes BOTH nondeterminisms the original harness left
    free-raced: (1) which child finalizes first, and (2) the on-disk Qdrant lock artifact, where a
    loser whose lazy-open lost the storage lock raised a RuntimeError that classified 'unknown' ->
    non-terminal -> Ok(Pending) and was mis-scored as a WIN (the two-winner flake). A WIN now
    additionally requires an Ok whose value is FINAL, so a stuck-Pending can never count as an
    effective winner. The correct reference is deterministically one-winner (loser fenced) while
    non_atomic_cas (fence off) still lets the loser mutate+finalize -> two winners, every run."""
    done = f"os.path.join({str(barrier_dir)!r}, 'winner_done')"
    if role == "winner":
        cp = "def _cp(name):\n    pass\n"
        after = (
            # RELEASE the on-disk Qdrant single-writer lock so the loser can open the shared store,
            # THEN publish the rendezvous signal (order matters: signal only after the lock is free).
            "try:\n"
            "    if _c._client is not None: _c._client.close()\n"
            "except Exception:\n"
            "    pass\n"
            f"open({done}, 'w').close()\n"
        )
    else:  # loser: block at the atomic boundary until the winner has finalized AND freed Qdrant
        cp = (
            "def _cp(name):\n"
            "    if name == 'before_pending_commit':\n"
            f"        for _ in range(int({_RACE_BARRIER_TIMEOUT!r} / 0.02)):\n"
            f"            if os.path.exists({done}): break\n"
            "            _time.sleep(0.02)\n"
            "        else:\n"
            "            raise SystemExit('r22 winner_done rendezvous timeout')\n"
        )
        after = ""
    return (
        "import os, time as _time, warnings\n"
        "from qdrant_client import QdrantClient\n"
        "from musubi.types.common import Ok\n"
        "from tests.lifecycle.test_c6b_atomicity import _RefCoordinator as _Coord, _RefIntent as _Intent\n"
        f"_c = _Coord(db_path={str(db_path)!r}, qdrant_path={str(qdrant_path)!r}, mode={mode!r})\n"
        + cp
        + "_c._checkpoint = _cp\n"
        f"_res = _c.transition(_Intent(collection={seed.collection!r}, object_id={seed.object_id!r}, "
        f"namespace={seed.namespace!r}, expected_version={seed.version}, target_state={target!r}, "
        f"actor='t', reason='r', operation_key={op_key!r}))\n"
        + after
        + "code = getattr(getattr(_res, 'error', None), 'code', None)\n"
        # a WIN is an Ok that actually FINALIZED (not a stuck Ok(Pending) - the old phantom winner).
        "_won = isinstance(_res, Ok) and getattr(_res.value, 'kind', None) == 'final'\n"
        f"os._exit({win_code} if _won else "
        f"({_R22_CONFLICT} if code in ('active_intent_exists', 'operation_key_conflict') else "
        f"({_R22_FENCE} if code == 'version_fence_violation' else 99)))\n"
    )


def _r22_assert_exact_outcome(codes: list[int]) -> None:
    """The ONE correct DETERMINISTIC R22 outcome (Yua): the winner/loser rendezvous forces the winner
    (op-a22, `codes[0]`) to FINALIZE and the loser (op-b22, `codes[1]`) to insert PAST the FINAL winner and
    be version-fenced AT APPLY, so the only correct result is exactly ``[WIN_A, FENCE]``. This is stricter
    than "one win + one conflict-or-fence": a begin-CONFLICT (loser rejected before the winner finalized -
    the rendezvous broke), a ROLE REVERSAL (B wins / A fenced), TWO winners (A, B - a lost update), or an
    UNKNOWN code each falsely-greened the apply-fence proof under the old len()-based check and must fail."""
    if codes != [_R22_WIN_A, _R22_FENCE]:
        raise DefectStillPresent(
            f"correct R22 must be exactly [winner op-a22 FINAL, loser op-b22 APPLY-fenced] = "
            f"[{_R22_WIN_A}, {_R22_FENCE}]; got {codes} (a two-winner lost-update, a begin-conflict instead "
            "of the deterministic apply fence, or a role reversal)"
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
    # DETERMINISTIC roles (Yua flake-harden): the "winner" (op-a22 -> matured) runs its full
    # transition and frees Qdrant before signalling; the "loser" (op-b22 -> demoted) blocks at the
    # atomic boundary until then, so it inserts past the (FINAL) winner's ux_active_intent and is
    # version-fenced against the winner's already-applied version. Pin the winner as op-a22.
    specs = (
        ("winner", "matured", "op-a22", _R22_WIN_A),
        ("loser", "demoted", "op-b22", _R22_WIN_B),
    )
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _r22_child_source(
                    role=role,
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
        for role, target, op_key, win_code in specs
    ]
    # Deterministic outcome collection: wait on each child BY IDENTITY (not arrival order). With the
    # rendezvous the winner always finalizes (codes[0]==_R22_WIN_A) and the loser is always fenced
    # (codes[1]==_R22_FENCE) for the correct reference; non_atomic_cas yields a second FINAL winner.
    codes = [p.wait(timeout=120) for p in procs]
    # The rendezvous makes the outcome DETERMINISTIC and SINGULAR: the winner (op-a22) finalizes and frees
    # the store, then the loser (op-b22) inserts PAST the FINAL winner and is version-fenced AT APPLY. So
    # the ONLY correct outcome is exactly [WIN_A, FENCE] (Yua) - a begin-conflict, a role reversal, or two
    # winners each falsely-green the apply-fence proof under the old len()-based check and must fail here.
    _r22_assert_exact_outcome(codes)
    winner_op, winner_target = "op-a22", "matured"  # locked by the deterministic rendezvous
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
    "needs exactly one winner that mutates while the loser is version-fenced AT APPLY - one FINAL row, one "
    "event, no overwrite. Proven DETERMINISTICALLY with two real processes: the winner (op-a22) finalizes "
    "and frees the store, then the loser (op-b22) inserts PAST the FINAL winner and its version-fenced "
    "apply against the winner's already-applied version is a guaranteed zero-match - the singular "
    "[winner-FINAL, loser-apply-fenced] outcome, not a begin-boundary race or a conflict-OR-fence."
)


def test_r22_two_process_race_one_winner_mutates_loser_fenced(tmp_path: Path) -> None:
    _check_r22(tmp_path)


def test_r22_outcome_validator_discriminates() -> None:
    """GREEN mechanism proof (Yua): the DETERMINISTIC R22 outcome validator accepts ONLY the singular
    [winner op-a22 FINAL, loser op-b22 APPLY-fenced] result and REJECTS - each at the exact-outcome
    assertion - a begin-conflict (rendezvous broke), a role reversal, two winners, and an unknown code.
    Without this, the old len(one-win)+len(one-conflict-or-fence) check would false-green a begin-conflict
    or role reversal and never actually prove the deterministic APPLY fence."""
    _r22_assert_exact_outcome(
        [_R22_WIN_A, _R22_FENCE]
    )  # the one correct deterministic outcome -> no raise
    for bad in (
        [
            _R22_WIN_A,
            _R22_CONFLICT,
        ],  # loser rejected at BEGIN, not the apply fence -> rendezvous broke
        [_R22_WIN_B, _R22_FENCE],  # role reversal: op-b22 won
        [_R22_WIN_B, _R22_CONFLICT],  # role reversal + begin-conflict
        [_R22_WIN_A, _R22_WIN_B],  # two winners = lost update
        [99, 99],  # unknown outcome codes
    ):
        with pytest.raises(DefectStillPresent):
            _r22_assert_exact_outcome(bad)


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
        except getattr(_api(), "CleanupConfigError", _CleanupConfigError):
            pass
        else:
            raise DefectStillPresent(f"cleanup must reject cutoff_epoch={bad_cut!r}")
    for bad_batch in (0, -1, 2.5, "x", True):
        try:
            coord.cleanup_terminal(cutoff_epoch=100.0, batch_limit=bad_batch)
        except getattr(_api(), "CleanupConfigError", _CleanupConfigError):
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
            "unknown_class_terminal",  # non-vacuity: maps unknown->terminal metric class (must be transient)
            "omit_log",  # non-vacuity: drops the required static failure log
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


# ============================================================================
# R21 RUNTIME REDS (P0b) — the three-way TransitionOutcome as it is actually
# EXERCISED, not just modelled (Yua: the reference adapter CANNOT be the acceptance
# surface). These reds drive the REAL routes/sweeps and monkeypatch only the
# `transition` seam each module imported, so the observed behaviour is the real
# handler's, while `src/musubi` stays absent.
#
#   TASK 1 — the four HTTP transition routes must map Ok(Pending) -> 202 typed body.
#   TASK 2 — the six maturation sweep callsites must DEFER a Pending (not count it,
#            not run dependent work, not retry, retain the ids).
#
# Every strict-xfail raises a DEDICATED DefectStillPresent for its own named missing
# behaviour ONLY; the unmarked discriminators prove each acceptance check catches the
# wrong shape AND passes the right one.
# ============================================================================


_R21_OPK = "opk-r21-pending"
_R21_EVENT_ID = "ev-r21-pending"


def _pending_outcome() -> Any:
    """An ``Ok(<pending>)`` shaped like the LOCKED ``TransitionPending`` (operation_key + event_id)."""
    return Ok(value=_RefPending(operation_key=_R21_OPK, event_id=_R21_EVENT_ID))


@dataclass(frozen=True)
class _RouteFinalLike:
    """Minimal ``TransitionResult``-shaped value the lifecycle handler reads attributes off. The
    archive/delete handlers ignore the value and return a hardcoded body, so any ``Ok`` maps 200 there."""

    object_id: str = "0" * 27
    object_type: str = "episodic"
    from_state: str = "provisional"
    to_state: str = "matured"
    version: int = 2


def _final_outcome() -> Any:
    return Ok(value=_RouteFinalLike())


def _err_outcome() -> Any:
    return Err(error=TransitionError(code="illegal_transition", message="r21 control error"))


# ---- self-contained FastAPI harness (mirrors tests/api/conftest app_factory) ---------------------- #

_R21_ISSUER = "https://auth.example.test"


def _r21_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "qdrant_host": "qdrant",
            "qdrant_api_key": SecretStr("test-qdrant-key"),
            "tei_dense_url": AnyHttpUrl("http://tei-dense"),
            "tei_sparse_url": AnyHttpUrl("http://tei-sparse"),
            "tei_reranker_url": AnyHttpUrl("http://tei-reranker"),
            "ollama_url": AnyHttpUrl("http://ollama:11434"),
            "embedding_model": "BAAI/bge-m3",
            "sparse_model": "naver/splade-v3",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "llm_model": "qwen2.5:7b-instruct-q4_K_M",
            "vault_path": tmp_path / "vault",
            "artifact_blob_path": tmp_path / "artifacts",
            "lifecycle_sqlite_path": tmp_path / "lifecycle.sqlite",
            "log_dir": tmp_path / "logs",
            "jwt_signing_key": SecretStr("a-very-long-test-signing-key-for-hs256-tokens-32+bytes"),
            "oauth_authority": AnyHttpUrl(_R21_ISSUER),
            "musubi_skip_bootstrap": True,
        }
    )


def _r21_token(settings: Settings, *, scopes: list[str], presence: str = "eric/claude-code") -> str:
    now = datetime.now(UTC)
    payload = {
        "iss": _R21_ISSUER,
        "sub": "eric-claude-code",
        "aud": "musubi",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "jti": "r21-token",
        "scope": " ".join(scopes),
        "presence": presence,
    }
    return jwt.encode(payload, settings.jwt_signing_key.get_secret_value(), algorithm="HS256")


@dataclass
class _RouteEnv:
    client: TestClient
    settings: Settings
    qdrant: QdrantClient
    episodic: EpisodicPlane
    curated: CuratedPlane
    concept: ConceptPlane
    artifact: ArtifactPlane


def _reset_api_globals() -> None:
    from musubi.api.idempotency import _GLOBAL_CACHE, _GLOBAL_LEASE_CACHE
    from musubi.api.rate_limit import _GLOBAL_LIMITER

    _GLOBAL_LIMITER.reset_for_test()
    _GLOBAL_CACHE._entries.clear()
    _GLOBAL_LEASE_CACHE._entries.clear()


@pytest.fixture
def route_env(tmp_path: Path) -> Iterator[_RouteEnv]:
    """Self-contained FastAPI app + in-memory Qdrant + planes. The TestClient is built with
    ``raise_server_exceptions=False`` so a handler that mishandles Pending yields a clean non-202
    Response (e.g. 500) instead of leaking an exception into the red — the red then raises a DEDICATED
    ``DefectStillPresent`` for "route X does not map Pending -> 202", never an unrelated exception."""
    settings = _r21_settings(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        qdrant = QdrantClient(":memory:")
    bootstrap(qdrant)
    embedder = FakeEmbedder()
    episodic = EpisodicPlane(client=qdrant, embedder=embedder)
    curated = CuratedPlane(client=qdrant, embedder=embedder)
    concept = ConceptPlane(client=qdrant, embedder=embedder)
    artifact = ArtifactPlane(client=qdrant, embedder=embedder)
    app = create_app(settings=settings)
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_qdrant_client] = lambda: qdrant
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_reranker] = lambda: embedder
    app.dependency_overrides[get_episodic_plane] = lambda: episodic
    app.dependency_overrides[get_curated_plane] = lambda: curated
    app.dependency_overrides[get_concept_plane] = lambda: concept
    app.dependency_overrides[get_artifact_plane] = lambda: artifact
    app.dependency_overrides[get_lifecycle_service] = lambda: object()
    _reset_api_globals()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield _RouteEnv(client, settings, qdrant, episodic, curated, concept, artifact)
    _reset_api_globals()
    qdrant.close()


def _patch_transition(monkeypatch: pytest.MonkeyPatch, dotted: str, outcome: Any) -> None:
    """Replace the ``transition`` name imported into a router module with a stub returning ``outcome``."""

    def _fake(*_args: Any, **_kwargs: Any) -> Any:
        return outcome

    monkeypatch.setattr(dotted, _fake)


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:  # a 500 with a non-JSON body — keep the raw text visible in the red message
        return {"_raw": response.text}


def _seed_episodic_row(env: _RouteEnv, ns: str, content: str) -> str:
    saved = asyncio.run(env.episodic.create(EpisodicMemory(namespace=ns, content=content)))
    return str(saved.object_id)


def _seed_artifact_row(env: _RouteEnv, ns: str) -> str:
    saved = asyncio.run(
        env.artifact.create(
            SourceArtifact(
                namespace=ns,
                title="r21-archive-target",
                filename="r21.txt",
                sha256=hashlib.sha256(b"r21").hexdigest(),
                content_type="text/plain",
                size_bytes=3,
                chunker="markdown-headings-v1",
            )
        )
    )
    return str(saved.object_id)


def _seed_curated_row(env: _RouteEnv, ns: str) -> str:
    saved = asyncio.run(
        env.curated.create(
            CuratedKnowledge(
                namespace=ns,
                title="r21-delete-target",
                content="r21 curated body",
                vault_path="curated/r21.md",
                body_hash=hashlib.sha256(b"r21-curated").hexdigest(),
            )
        )
    )
    return str(saved.object_id)


def _drive_route(
    env: _RouteEnv, monkeypatch: pytest.MonkeyPatch, route: str, outcome: Any
) -> tuple[int, Any]:
    """Seed a real row, patch the route's ``transition`` seam to return ``outcome``, invoke the REAL
    authed handler, and return ``(status_code, body)``."""
    if route == "lifecycle":
        ns = "eric/claude-code/episodic"
        oid = _seed_episodic_row(env, ns, "r21-transition-target")
        _patch_transition(monkeypatch, "musubi.api.routers.writes_lifecycle.transition", outcome)
        token = _r21_token(env.settings, scopes=["operator"])
        r = env.client.post(
            "/v1/lifecycle/transition",
            headers={"Authorization": f"Bearer {token}"},
            json={"object_id": oid, "to_state": "matured", "actor": "r21", "reason": "r21-map"},
        )
        return r.status_code, _safe_json(r)
    if route == "artifact":
        ns = "eric/claude-code/artifact"
        oid = _seed_artifact_row(env, ns)
        _patch_transition(monkeypatch, "musubi.api.routers.writes_artifact.transition", outcome)
        token = _r21_token(env.settings, scopes=[f"{ns}:rw"])
        r = env.client.post(
            f"/v1/artifacts/{oid}/archive",
            headers={"Authorization": f"Bearer {token}"},
            params={"namespace": ns},
        )
        return r.status_code, _safe_json(r)
    if route == "curated":
        ns = "eric/claude-code/curated"
        oid = _seed_curated_row(env, ns)
        _patch_transition(monkeypatch, "musubi.api.routers.writes_curated.transition", outcome)
        token = _r21_token(env.settings, scopes=[f"{ns}:rw"])
        r = env.client.delete(
            f"/v1/curated/{oid}",
            headers={"Authorization": f"Bearer {token}"},
            params={"namespace": ns},
        )
        return r.status_code, _safe_json(r)
    if route == "episodic":
        ns = "eric/claude-code/episodic"
        oid = _seed_episodic_row(env, ns, "r21-soft-delete-target")
        _patch_transition(monkeypatch, "musubi.api.routers.writes_episodic.transition", outcome)
        token = _r21_token(env.settings, scopes=[f"{ns}:rw"])
        r = env.client.delete(
            f"/v1/episodic/{oid}",
            headers={"Authorization": f"Bearer {token}"},
            params={"namespace": ns},
        )
        return r.status_code, _safe_json(r)
    raise AssertionError(f"unknown route {route!r}")


#: Fields that belong to a Final (applied) outcome ONLY. A Pending 202 body carrying any of these is
#: fabricating a success payload the coordinator does not have yet (Yua ruling 3). The route must NOT
#: widen the Final shape onto a Pending.
_FINAL_ONLY_BODY_FIELDS = ("object_id", "from_state", "to_state", "version")


def _pending_response_ok(status: int, body: Any) -> bool:
    """The acceptance check for the Pending->202 contract (Yua ruling 3 — corrected): HTTP 202 + a typed
    body with ``status="pending"`` AND both identifiers present as NON-EMPTY ``str`` values, AND carrying
    NONE of the Final-only fields (no fabricated applied payload). Presence of an id key alone is not
    enough — a route that emits ``operation_key=None``/``""`` or a Final field has not honored the
    contract."""
    if status != 202 or not isinstance(body, dict):
        return False
    if body.get("status") != "pending":
        return False
    opk = body.get("operation_key")
    ev = body.get("event_id")
    if not (isinstance(opk, str) and opk != ""):
        return False
    if not (isinstance(ev, str) and ev != ""):
        return False
    return not any(field in body for field in _FINAL_ONLY_BODY_FIELDS)


# ---- TASK 1 reds: one strict-xfail per REAL transition route (Pending -> 202 typed body) ----------- #


def test_r21_route_lifecycle_pending_maps_to_202(
    route_env: _RouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body = _drive_route(route_env, monkeypatch, "lifecycle", _pending_outcome())
    if not _pending_response_ok(status, body):
        raise DefectStillPresent(
            f"route lifecycle does not map Pending to HTTP 202 typed body; got {status}/{body!r}"
        )


def test_r21_route_artifact_pending_maps_to_202(
    route_env: _RouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body = _drive_route(route_env, monkeypatch, "artifact", _pending_outcome())
    if not _pending_response_ok(status, body):
        raise DefectStillPresent(
            f"route artifact does not map Pending to HTTP 202 typed body; got {status}/{body!r}"
        )


def test_r21_route_curated_pending_maps_to_202(
    route_env: _RouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body = _drive_route(route_env, monkeypatch, "curated", _pending_outcome())
    if not _pending_response_ok(status, body):
        raise DefectStillPresent(
            f"route curated does not map Pending to HTTP 202 typed body; got {status}/{body!r}"
        )


def test_r21_route_episodic_pending_maps_to_202(
    route_env: _RouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body = _drive_route(route_env, monkeypatch, "episodic", _pending_outcome())
    if not _pending_response_ok(status, body):
        raise DefectStillPresent(
            f"route episodic does not map Pending to HTTP 202 typed body; got {status}/{body!r}"
        )


# ---- TASK 1 green controls: the harness itself is correct — only the Pending branch is missing ------ #


@pytest.mark.parametrize("route", ["lifecycle", "artifact", "curated", "episodic"])
def test_r21_route_controls_final_200_and_err_typed(
    route: str, route_env: _RouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GREEN control (unmarked): with the SAME harness, a monkeypatched Final maps to the existing 200
    typed body and a monkeypatched Err maps to the existing typed error (400). This proves the harness
    drives the real route correctly and isolates the missing behaviour to the Pending branch alone."""
    status_final, _ = _drive_route(route_env, monkeypatch, route, _final_outcome())
    assert status_final == 200, (
        f"{route}: Final must map to the existing 200 success, got {status_final}"
    )

    status_err, body_err = _drive_route(route_env, monkeypatch, route, _err_outcome())
    assert status_err == 400, f"{route}: Err must map to the existing typed 400, got {status_err}"
    assert isinstance(body_err, dict) and body_err.get("error", {}).get("code") == "BAD_REQUEST", (
        f"{route}: Err body must be the existing typed error, got {body_err!r}"
    )


# ---- TASK 1 discriminator: the Pending acceptance check catches each failure mode SEPARATELY -------- #


def _correct_route_map(kind: str) -> tuple[int, dict[str, Any]]:
    """A reference route-outcome mapper (the CORRECT S7 mapping) used only as a discriminator oracle."""
    if kind == "final":
        return 200, {
            "object_id": "0" * 27,
            "from_state": "provisional",
            "to_state": "matured",
            "version": 2,
        }
    if kind == "pending":
        return 202, {"status": "pending", "operation_key": _R21_OPK, "event_id": _R21_EVENT_ID}
    if kind == "err":
        return 400, {"error": {"code": "BAD_REQUEST", "detail": "illegal transition"}}
    raise AssertionError(f"unknown kind {kind!r}")


def test_r21_route_pending_check_discriminates_each_failure_mode() -> None:
    """GREEN mechanism proof (unmarked): the ``_pending_response_ok`` acceptance check passes the correct
    (202 + status=pending + both NON-EMPTY str ids + no Final fields) shape and independently REJECTS each
    wrong shape — wrong-status, pending-as-final, pending-as-error, a dropped identifier, an id that is
    None/empty/wrong-type (Yua ruling 3), and a 202 body that fabricates a Final field."""
    # the correct Pending mapping passes
    assert _pending_response_ok(*_correct_route_map("pending"))
    # wrong-status: right body, wrong code
    assert not _pending_response_ok(
        200, {"status": "pending", "operation_key": _R21_OPK, "event_id": _R21_EVENT_ID}
    )
    assert not _pending_response_ok(
        500, {"status": "pending", "operation_key": _R21_OPK, "event_id": _R21_EVENT_ID}
    )
    # pending-as-final: mapped like a Final (200, success body, no pending discriminator)
    assert not _pending_response_ok(*_correct_route_map("final"))
    # pending-as-error: mapped to an Err status/body
    assert not _pending_response_ok(*_correct_route_map("err"))
    # dropped-identifier: 202 + status=pending but one id missing
    assert not _pending_response_ok(202, {"status": "pending", "event_id": _R21_EVENT_ID})
    assert not _pending_response_ok(202, {"status": "pending", "operation_key": _R21_OPK})
    # id present but None / empty / wrong-type — presence is not enough (Yua ruling 3)
    assert not _pending_response_ok(
        202, {"status": "pending", "operation_key": None, "event_id": _R21_EVENT_ID}
    )
    assert not _pending_response_ok(
        202, {"status": "pending", "operation_key": "", "event_id": _R21_EVENT_ID}
    )
    assert not _pending_response_ok(
        202, {"status": "pending", "operation_key": 123, "event_id": _R21_EVENT_ID}
    )
    assert not _pending_response_ok(
        202, {"status": "pending", "operation_key": _R21_OPK, "event_id": None}
    )
    assert not _pending_response_ok(
        202, {"status": "pending", "operation_key": _R21_OPK, "event_id": ""}
    )
    # a 202 pending body that fabricates a Final-only field (each rejected independently)
    for _final_field in _FINAL_ONLY_BODY_FIELDS:
        assert not _pending_response_ok(
            202,
            {
                "status": "pending",
                "operation_key": _R21_OPK,
                "event_id": _R21_EVENT_ID,
                _final_field: "fabricated",
            },
        ), f"a Pending 202 body carrying the Final-only field {_final_field!r} must be rejected"


# ---- TASK 1 (Yua ruling 4): a REAL typed Pending body schema + a per-route red validating against it -- #


class _PendingBodySchema(BaseModel):
    """The typed Pending HTTP body the four transition routes must return on ``Ok(Pending)`` (Yua ruling
    4). ``extra="forbid"`` rejects any fabricated Final field (``object_id``/``from_state``/``to_state``/
    ``version``) or any other widening; the validator rejects empty identifiers. This is the schema the
    source's Pydantic response model must be assignable to WITHOUT widening the existing Final/Err shapes."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending"]
    operation_key: str
    event_id: str

    @field_validator("operation_key", "event_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("identifier must be a non-empty string")
        return value


def _pending_body_validates(status: int, body: Any) -> bool:
    """True iff ``body`` is a 202 payload that VALIDATES against the typed Pending schema (extra=forbid,
    non-empty ids). Never raises — a ValidationError (missing/empty/extra-Final/wrong-status) or a
    non-dict body simply fails the check, so the red raises a DEDICATED DefectStillPresent, not a leaked
    ValidationError."""
    if status != 202 or not isinstance(body, dict):
        return False
    try:
        _PendingBodySchema.model_validate(body)
    except ValidationError:
        return False
    return True


_R21_OPENAPI_PATHS = {
    "lifecycle": ("/v1/lifecycle/transition", "post"),
    "artifact": ("/v1/artifacts/{object_id}/archive", "post"),
    "curated": ("/v1/curated/{object_id}", "delete"),
    "episodic": ("/v1/episodic/{object_id}", "delete"),
}


def _pending_openapi_declared(client: TestClient, route: str) -> bool:
    """True iff the route declares its 202 body as exactly TransitionPendingBody."""
    path, method = _R21_OPENAPI_PATHS[route]
    doc = client.get("/v1/openapi.json").json()
    schema = (
        doc.get("paths", {})
        .get(path, {})
        .get(method, {})
        .get("responses", {})
        .get("202", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    return bool(schema == {"$ref": "#/components/schemas/TransitionPendingBody"})


@pytest.mark.parametrize("route", ["lifecycle", "artifact", "curated", "episodic"])
def test_r21_route_pending_body_matches_typed_schema(
    route: str, route_env: _RouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, body = _drive_route(route_env, monkeypatch, route, _pending_outcome())
    if not _pending_body_validates(status, body):
        raise DefectStillPresent(
            f"route {route} does not return a 202 body validating against the typed Pending schema "
            f"(status=pending + non-empty str operation_key/event_id + no Final fields); got {status}/{body!r}"
        )
    if not _pending_openapi_declared(route_env.client, route):
        raise DefectStillPresent(
            f"route {route} does not declare HTTP 202 as TransitionPendingBody in runtime OpenAPI"
        )


def test_r21_pending_body_schema_discriminates() -> None:
    """GREEN mechanism proof (unmarked): the typed Pending schema ACCEPTS the correct pending body and
    REJECTS each wrong dict independently — a missing field, an empty-string id, an extra Final field
    (``extra='forbid'``), and a wrong ``status`` literal."""
    correct = {"status": "pending", "operation_key": _R21_OPK, "event_id": _R21_EVENT_ID}
    assert _PendingBodySchema.model_validate(correct).status == "pending"
    assert _pending_body_validates(202, correct)
    # a 200 (right body, wrong status code) is not a valid Pending response
    assert not _pending_body_validates(200, correct)
    # missing event_id
    with pytest.raises(ValidationError):
        _PendingBodySchema.model_validate({"status": "pending", "operation_key": _R21_OPK})
    # empty-string identifier
    with pytest.raises(ValidationError):
        _PendingBodySchema.model_validate(
            {"status": "pending", "operation_key": "", "event_id": _R21_EVENT_ID}
        )
    # extra Final field rejected by extra="forbid"
    for _final_field in _FINAL_ONLY_BODY_FIELDS:
        with pytest.raises(ValidationError):
            _PendingBodySchema.model_validate({**correct, _final_field: "fabricated"})
    # wrong status literal
    with pytest.raises(ValidationError):
        _PendingBodySchema.model_validate(
            {"status": "final", "operation_key": _R21_OPK, "event_id": _R21_EVENT_ID}
        )


# ============================================================================
# TASK 2 — six maturation path reds + a static callsite/branch inventory.
#
# The six sweep callsites of the primitive (maturation.py) each do
# `isinstance(result, Ok) -> transitioned++`, a TWO-way Ok/not-Ok branch with no
# Pending arm. Per the LOCKED contract, Pending means DEFERRED: not counted as a
# completed transition, no post-transition dependent work (esp. the :479 supersession
# back-link), no immediate direct retry, ids retained for the reconciler. Each red
# drives the REAL sweep with `transition` monkeypatched to return a Pending outcome
# and raises a DEDICATED DefectStillPresent for its own named missing behaviour.
# ============================================================================


class _TransitionSpy:
    """A stand-in for the primitive that records every call and returns a fixed outcome. Call count is the
    observable for "did post-transition dependent work run?" (the :479 back-link is a second call)."""

    def __init__(self, outcome: Any) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._outcome = outcome

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self._outcome


class _FakeOllama:
    """Deterministic in-process OllamaClient — no network. Constant importance, empty topics."""

    async def score_importance(self, items: list[OllamaImportance]) -> dict[str, int] | None:
        return {item.object_id: 8 for item in items}

    async def infer_topics(self, items: list[OllamaTopic]) -> dict[str, list[str]] | None:
        return {item.object_id: [] for item in items}


_: OllamaClient = _FakeOllama()  # sanity: the fake satisfies the Protocol


def _mat_config() -> MaturationConfig:
    return MaturationConfig()


async def _seed_provisional_episodic(
    plane: EpisodicPlane, qc: QdrantClient, ns: str, content: str, *, age_seconds: int
) -> str:
    """Create a provisional episodic row, back-dated so the sweep's age cutoff selects it."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content))
    backdate = datetime.now(UTC) - timedelta(seconds=age_seconds)
    qc.set_payload(
        collection_name="musubi_episodic",
        payload={
            "created_at": backdate.isoformat(),
            "created_epoch": backdate.timestamp(),
            "updated_at": backdate.isoformat(),
            "updated_epoch": backdate.timestamp(),
        },
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    return str(saved.object_id)


async def _seed_matured_episodic(
    plane: EpisodicPlane,
    qc: QdrantClient,
    coordinator: Any,
    ns: str,
    content: str,
    *,
    age_seconds: int,
) -> str:
    """Create + transition an episodic row to matured (real, pre-patch), back-dated by ``age_seconds``."""
    saved = await plane.create(EpisodicMemory(namespace=ns, content=content))
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="seed",
        reason="seed",
        coordinator=coordinator,
    )
    backdate = datetime.now(UTC) - timedelta(seconds=age_seconds)
    qc.set_payload(
        collection_name="musubi_episodic",
        payload={
            "created_at": backdate.isoformat(),
            "created_epoch": backdate.timestamp(),
            "updated_at": backdate.isoformat(),
            "updated_epoch": backdate.timestamp(),
        },
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    return str(saved.object_id)


async def _seed_synthesized_concept(
    plane: ConceptPlane, qc: QdrantClient, ns: str, *, reinforce: int, age_seconds: int
) -> str:
    saved = await plane.create(
        SynthesizedConcept(
            namespace=ns,
            title="r21 concept",
            content="r21 concept content",
            synthesis_rationale="r21 rationale",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    for _ in range(reinforce):
        await plane.reinforce(namespace=ns, object_id=saved.object_id)
    backdate = datetime.now(UTC) - timedelta(seconds=age_seconds)
    qc.set_payload(
        collection_name="musubi_concept",
        payload={"created_at": backdate.isoformat(), "created_epoch": backdate.timestamp()},
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    return str(saved.object_id)


async def _seed_matured_concept(
    plane: ConceptPlane,
    qc: QdrantClient,
    coordinator: Any,
    ns: str,
    *,
    age_seconds: int,
) -> str:
    saved = await plane.create(
        SynthesizedConcept(
            namespace=ns,
            title="r21 concept demote",
            content="r21 concept demote content",
            synthesis_rationale="r21 rationale",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    await plane.transition(
        namespace=ns,
        object_id=saved.object_id,
        to_state="matured",
        actor="seed",
        reason="seed",
        coordinator=coordinator,
    )
    backdate = datetime.now(UTC) - timedelta(seconds=age_seconds)
    qc.set_payload(
        collection_name="musubi_concept",
        payload={"updated_at": backdate.isoformat(), "updated_epoch": backdate.timestamp()},
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=saved.object_id)
                )
            ]
        ),
    )
    return str(saved.object_id)


def _mat_env(tmp_path: Path) -> tuple[QdrantClient, LifecycleEventSink, MaturationCursor]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        qc = QdrantClient(":memory:")
    bootstrap(qc)
    sink = LifecycleEventSink(db_path=tmp_path / "events.db", flush_every_n=10, flush_every_s=1.0)
    cursor = MaturationCursor(db_path=tmp_path / "cursor.db")
    return qc, sink, cursor


def _deferred_entry_ids_ok(entry: Any) -> bool:
    """Normalize ONE ``report.deferred`` entry defensively — a mapping (``.get``), a 2-tuple/list, or an
    object (``getattr``) — and return True iff it carries a NON-EMPTY ``operation_key`` AND ``event_id``
    (str). Never raises on an unexpected shape (returns False), so the red stays a DEDICATED
    DefectStillPresent rather than leaking an AttributeError."""
    if isinstance(entry, Mapping):
        opk, ev = entry.get("operation_key"), entry.get("event_id")
    elif isinstance(entry, tuple | list) and len(entry) >= 2:
        opk, ev = entry[0], entry[1]
    else:
        opk, ev = getattr(entry, "operation_key", None), getattr(entry, "event_id", None)
    return isinstance(opk, str) and opk != "" and isinstance(ev, str) and ev != ""


def _assert_pending_deferred(
    report: Any, spy: "_TransitionSpy", sweep: str, *, forward_calls: int = 1
) -> None:
    """DEDICATED red assertion covering the FULL internal-DEFERRED contract (docs §A, Yua ruling 1) for a
    single seeded Pending forward transition. Raises a ``DefectStillPresent`` naming WHICH sub-condition
    failed unless ALL hold:

    (c) EXACTLY ``forward_calls`` transition call happened for the row — no immediate direct retry and no
        post-transition dependent work (e.g. the supersession back-link) on a Pending forward;
    (b) ``report.transitioned == 0`` — a deferral is NOT counted as a completed transition;
    (a) ``getattr(report, "deferred", [])`` retains one PII-free entry with a NON-EMPTY ``operation_key``
        AND ``event_id`` for the reconciler.

    ``report.deferred`` is read via ``getattr`` (absent field -> ``[]``, never an AttributeError). Order is
    (c)->(b)->(a) so each of today's six reds fails at the sub-condition its decorator names."""
    # (c) exactly one transition call — no immediate retry, no dependent back-link on a Pending forward
    n_calls = len(getattr(spy, "calls", []))
    if n_calls != forward_calls:
        raise DefectStillPresent(
            f"{sweep}: a Pending forward transition triggered {n_calls} transition call(s), expected "
            f"exactly {forward_calls} — an immediate retry or post-transition dependent work (e.g. the "
            f"supersession back-link) ran on a Pending forward instead of deferring it to the reconciler"
        )
    # (b) not counted as a completed transition
    transitioned = getattr(report, "transitioned", -1)
    if transitioned != 0:
        raise DefectStillPresent(
            f"{sweep}: counts a Pending forward transition as a completed transition "
            f"(expected DEFERRED, transitioned==0); got transitioned={transitioned}"
        )
    # (a) retained in observable deferred accounting with non-empty ids
    deferred = getattr(report, "deferred", [])
    if not (isinstance(deferred, list | tuple) and len(deferred) >= 1):
        raise DefectStillPresent(
            f"{sweep}: a Pending forward transition is not retained in report.deferred "
            f"(expected one PII-free entry with operation_key + event_id for the reconciler); got {deferred!r}"
        )
    if not any(_deferred_entry_ids_ok(entry) for entry in deferred):
        raise DefectStillPresent(
            f"{sweep}: report.deferred has no entry carrying a NON-EMPTY operation_key AND event_id "
            f"(the ids the reconciler needs to finalize the deferred transition); got {deferred!r}"
        )


# ---- TASK 2 reds: one strict-xfail per distinct callsite shape (Pending must DEFER) ---------------- #


async def test_r21_maturation_episodic_defers_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qc, sink, cursor = _mat_env(tmp_path)
    try:
        plane = EpisodicPlane(client=qc, embedder=FakeEmbedder())
        ns = "eric/claude-code/episodic"
        await _seed_provisional_episodic(plane, qc, ns, "plain provisional row", age_seconds=7200)
        spy = _TransitionSpy(_pending_outcome())
        monkeypatch.setattr("musubi.lifecycle.maturation.transition", spy)
        report = await episodic_maturation_sweep(
            client=qc,
            sink=sink,
            coordinator=cast(Any, object()),
            ollama=_FakeOllama(),
            cursor=cursor,
            config=_mat_config(),
        )
        _assert_pending_deferred(report, spy, "episodic_maturation_sweep")
    finally:
        sink.close()
        qc.close()


async def test_r21_maturation_supersession_backlink_not_run_on_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qc, sink, cursor = _mat_env(tmp_path)
    try:
        plane = EpisodicPlane(client=qc, embedder=FakeEmbedder())
        ns = "eric/claude-code/episodic"
        # A matured predecessor whose content matches the correction row's needle, so
        # _find_supersession_candidate resolves it and the back-link path is reached.
        await _seed_matured_episodic(
            plane,
            qc,
            _coordinator(qc, sink._db_path),
            ns,
            "shared gpu upgrade note",
            age_seconds=3600,
        )
        await _seed_provisional_episodic(
            plane, qc, ns, "correction: shared gpu upgrade note", age_seconds=7200
        )
        spy = _TransitionSpy(_pending_outcome())
        monkeypatch.setattr("musubi.lifecycle.maturation.transition", spy)
        report = await episodic_maturation_sweep(
            client=qc,
            sink=sink,
            coordinator=cast(Any, object()),
            ollama=_FakeOllama(),
            cursor=cursor,
            config=_mat_config(),
        )
        # forward_calls=1 pins the (d) sub-condition: the supersession back-link (a SECOND transition on
        # the predecessor) is post-transition dependent work that must NOT fire on a Pending forward.
        _assert_pending_deferred(
            report, spy, "episodic_maturation_sweep supersession", forward_calls=1
        )
    finally:
        sink.close()
        qc.close()


async def test_r21_maturation_provisional_ttl_defers_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qc, sink, _cursor = _mat_env(tmp_path)
    try:
        plane = EpisodicPlane(client=qc, embedder=FakeEmbedder())
        ns = "eric/claude-code/episodic"
        await _seed_provisional_episodic(plane, qc, ns, "ttl row", age_seconds=8 * 86400)
        spy = _TransitionSpy(_pending_outcome())
        monkeypatch.setattr("musubi.lifecycle.maturation.transition", spy)
        report = await provisional_ttl_sweep(
            client=qc, sink=sink, coordinator=cast(Any, object()), config=_mat_config()
        )
        _assert_pending_deferred(report, spy, "provisional_ttl_sweep")
    finally:
        sink.close()
        qc.close()


async def test_r21_maturation_episodic_demotion_defers_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qc, sink, _cursor = _mat_env(tmp_path)
    try:
        plane = EpisodicPlane(client=qc, embedder=FakeEmbedder())
        ns = "eric/claude-code/episodic"
        await _seed_matured_episodic(
            plane,
            qc,
            _coordinator(qc, sink._db_path),
            ns,
            "demote row",
            age_seconds=31 * 86400,
        )
        spy = _TransitionSpy(_pending_outcome())
        monkeypatch.setattr("musubi.lifecycle.maturation.transition", spy)
        report = await episodic_demotion_sweep(
            client=qc, sink=sink, coordinator=cast(Any, object()), config=_mat_config()
        )
        _assert_pending_deferred(report, spy, "episodic_demotion_sweep")
    finally:
        sink.close()
        qc.close()


async def test_r21_maturation_concept_defers_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qc, sink, _cursor = _mat_env(tmp_path)
    try:
        plane = ConceptPlane(client=qc, embedder=FakeEmbedder())
        ns = "eric/claude-code/concept"
        await _seed_synthesized_concept(plane, qc, ns, reinforce=3, age_seconds=2 * 86400)
        spy = _TransitionSpy(_pending_outcome())
        monkeypatch.setattr("musubi.lifecycle.maturation.transition", spy)
        report = await concept_maturation_sweep(
            client=qc, sink=sink, coordinator=cast(Any, object()), config=_mat_config()
        )
        _assert_pending_deferred(report, spy, "concept_maturation_sweep")
    finally:
        sink.close()
        qc.close()


async def test_r21_maturation_concept_demotion_defers_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qc, sink, _cursor = _mat_env(tmp_path)
    try:
        plane = ConceptPlane(client=qc, embedder=FakeEmbedder())
        ns = "eric/claude-code/concept"
        await _seed_matured_concept(
            plane,
            qc,
            _coordinator(qc, sink._db_path),
            ns,
            age_seconds=40 * 86400,
        )
        spy = _TransitionSpy(_pending_outcome())
        monkeypatch.setattr("musubi.lifecycle.maturation.transition", spy)
        report = await concept_demotion_sweep(
            client=qc, sink=sink, coordinator=cast(Any, object()), config=_mat_config()
        )
        _assert_pending_deferred(report, spy, "concept_demotion_sweep")
    finally:
        sink.close()
        qc.close()


# ---- TASK 2 discriminator: the FULL-contract acceptance catches each new false candidate ------------ #


class _OracleSweepReport:
    """A reference SweepReport-shaped oracle (no src) carrying just the two fields the full-defer
    acceptance reads. Used to red-proof each NEW false candidate against ``_assert_pending_deferred``."""

    def __init__(self, transitioned: int, deferred: list[Any]) -> None:
        self.transitioned = transitioned
        self.deferred = deferred


def _fake_spy(n_calls: int) -> _TransitionSpy:
    """A ``_TransitionSpy`` pre-loaded with ``n_calls`` recorded calls (the call count is the observable
    the acceptance reads for immediate-retry / dependent-work)."""
    spy = _TransitionSpy(_pending_outcome())
    spy.calls = [((), {}) for _ in range(n_calls)]
    return spy


def test_r21_full_defer_acceptance_discriminates() -> None:
    """GREEN mechanism proof (unmarked): ``_assert_pending_deferred`` ACCEPTS the correct full-defer shape
    (dict-entry AND object-entry, proving getattr/.get normalization) and raises a DEDICATED
    DefectStillPresent naming the failing sub-condition for each NEW false candidate — stops-incrementing-
    but-drops-ids, stops-but-immediately-retries, stops-but-runs-dependent-work, counts-pending-as-
    completed, and drops-the-deferred-row entirely."""
    entry = {"operation_key": _R21_OPK, "event_id": _R21_EVENT_ID}

    # correct full-defer shape passes — dict entry (via .get)
    _assert_pending_deferred(_OracleSweepReport(0, [entry]), _fake_spy(1), "correct-dict")
    # correct full-defer shape passes — OBJECT entry (via getattr), proving defensive normalization
    _assert_pending_deferred(
        _OracleSweepReport(0, [_RefPending(operation_key=_R21_OPK, event_id=_R21_EVENT_ID)]),
        _fake_spy(1),
        "correct-obj",
    )

    # stops-incrementing-but-drops-ids: deferred row present, transitioned==0, one call, but event_id ""
    with pytest.raises(DefectStillPresent, match="NON-EMPTY operation_key AND event_id"):
        _assert_pending_deferred(
            _OracleSweepReport(0, [{"operation_key": _R21_OPK, "event_id": ""}]),
            _fake_spy(1),
            "drops-ids",
        )
    # stops-but-immediately-retries: a SECOND transition call (retry) on the Pending forward
    with pytest.raises(DefectStillPresent, match="expected exactly 1"):
        _assert_pending_deferred(_OracleSweepReport(0, [entry]), _fake_spy(2), "retries")
    # stops-but-runs-dependent-work: a SECOND transition call (the back-link) on the Pending forward
    with pytest.raises(DefectStillPresent, match="dependent work"):
        _assert_pending_deferred(_OracleSweepReport(0, [entry]), _fake_spy(2), "dependent-work")
    # counts-pending-as-completed: transitioned incremented for a deferral
    with pytest.raises(DefectStillPresent, match="completed transition"):
        _assert_pending_deferred(_OracleSweepReport(1, [entry]), _fake_spy(1), "counts-pending")
    # drops-the-deferred-row entirely: nothing retained for the reconciler
    with pytest.raises(DefectStillPresent, match="not retained"):
        _assert_pending_deferred(_OracleSweepReport(0, []), _fake_spy(1), "drops-row")
    # a report with NO `deferred` field at all (today's SweepReport): getattr -> [] is treated as "not
    # retained", never an AttributeError — proving the shape-normalization guard
    no_deferred = SimpleNamespace(transitioned=0)
    with pytest.raises(DefectStillPresent, match="not retained"):
        _assert_pending_deferred(no_deferred, _fake_spy(1), "no-deferred-field")


# ---- TASK 2: static callsite/branch inventory over maturation.py ----------------------------------- #

_MATURATION_REL = "lifecycle/maturation.py"

#: The reviewed present denominator: EXACTLY the six transition() callsites, pinned by enclosing sweep
#: function (with multiplicity — episodic_maturation_sweep has both the forward :458 and the back-link
#: :479). A skipped/added callsite fails the unmarked control loudly (like G1's present-denominator).
_EXPECTED_MATURATION_CALLSITES: Counter[str] = Counter(
    {
        "episodic_maturation_sweep": 2,
        "provisional_ttl_sweep": 1,
        "episodic_demotion_sweep": 1,
        "concept_maturation_sweep": 1,
        "concept_demotion_sweep": 1,
    }
)


def _maturation_transition_callsites() -> list[tuple[str, ast.AST]]:
    """(enclosing-sweep-name, enclosing-func-node) for every bare ``transition(...)`` call in maturation.py."""
    tree = ast.parse((_SRC / _MATURATION_REL).read_text())
    parent = _parent_map(tree)
    out: list[tuple[str, ast.AST]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "transition":
            cur = parent.get(n)
            while cur is not None and not isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
                cur = parent.get(cur)
            if cur is not None:
                out.append((getattr(cur, "name", "<module>"), cur))
    return out


def _maturation_transition_call_nodes(
    tree: ast.AST, parent: dict[ast.AST, ast.AST]
) -> list[tuple[str, ast.AST, ast.Call]]:
    """(enclosing-sweep-name, enclosing-func-node, call-node) for every bare ``transition(...)`` call."""
    out: list[tuple[str, ast.AST, ast.Call]] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "transition":
            cur = parent.get(n)
            while cur is not None and not isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
                cur = parent.get(cur)
            if cur is not None:
                out.append((getattr(cur, "name", "<module>"), cur, n))
    return out


def _refs_name(node: ast.AST, name: str) -> bool:
    """True iff ``node`` (recursively) references a simple ``Name`` equal to ``name`` — e.g. ``result`` is
    referenced by ``result``, ``result.value``, ``isinstance(result.value, X)``."""
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _test_is_pending_discriminator(test: ast.AST) -> bool:
    """True iff a boolean ``test`` looks like a Pending/deferred discriminator — an
    ``isinstance(..., TransitionPending)`` / a Name or Attribute containing 'pending'/'deferred' / a
    'pending'/'deferred' string literal (mirrors the coarse AST vocabulary used elsewhere)."""
    for n in ast.walk(test):
        if isinstance(n, ast.Name) and ("pending" in n.id.lower() or "deferred" in n.id.lower()):
            return True
        if isinstance(n, ast.Attribute) and (
            "pending" in n.attr.lower() or "deferred" in n.attr.lower()
        ):
            return True
        if (
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and n.value.strip().lower() in {"pending", "deferred"}
        ):
            return True
    return False


def _pending_arm_ok_for_callsite(
    func: ast.AST, call: ast.Call, parent: dict[ast.AST, ast.AST]
) -> tuple[bool, str]:
    """Per-CALLSITE structural check (Yua ruling 2 — corrected from the function-scoped heuristic):
    resolve the result variable ``call`` is assigned to, then prove the enclosing sweep consumes THAT SAME
    result with an EXPLICIT Pending arm — an ``if`` whose test references the result AND is a Pending
    discriminator — placed AFTER the callsite and BEFORE the callsite's success/dependent-work path (the
    earliest of a ``transitioned += ...`` or a subsequent dependent ``transition(...)`` call). Returns
    ``(ok, detail)``."""
    # 1. resolve the assigned result name (a callsite whose result is not bound to a simple name fails)
    owner = parent.get(call)
    if isinstance(owner, ast.Await):
        owner = parent.get(owner)
    if not (
        isinstance(owner, ast.Assign)
        and len(owner.targets) == 1
        and isinstance(owner.targets[0], ast.Name)
    ):
        return (
            False,
            "transition() result is not bound to a single simple name (cannot trace a consumer)",
        )
    result_name = owner.targets[0].id
    call_line = call.lineno

    # 2. the success/dependent-work marker: the earliest line AFTER the callsite that either increments
    #    `transitioned` or issues another dependent `transition(...)` (e.g. the supersession back-link).
    marker_line: float = math.inf
    for n in ast.walk(func):
        line = getattr(n, "lineno", None)
        if line is None or line <= call_line:
            continue
        counts_transition = (
            isinstance(n, ast.AugAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "transitioned"
        )
        dependent_call = (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "transition"
            and n is not call
        )
        if counts_transition or dependent_call:
            marker_line = min(marker_line, line)

    # 3. a valid Pending arm: an `if` on THIS result, a Pending discriminator, after the callsite and
    #    before the success/dependent-work marker.
    for n in ast.walk(func):
        if not isinstance(n, ast.If):
            continue
        if n.lineno <= call_line or n.lineno >= marker_line:
            continue
        if _refs_name(n.test, result_name) and _test_is_pending_discriminator(n.test):
            return True, "ok"
    return (
        False,
        f"no explicit Pending arm on result {result_name!r} before the success/dependent-work path "
        f"(callsite line {call_line}, success/dependent path at line "
        f"{'none' if marker_line == math.inf else int(marker_line)})",
    )


def test_r21_maturation_callsite_pending_arm_inventory() -> None:
    tree = ast.parse((_SRC / _MATURATION_REL).read_text())
    parent = _parent_map(tree)
    unhandled: list[str] = []
    for fn, func, call in _maturation_transition_call_nodes(tree, parent):
        ok, detail = _pending_arm_ok_for_callsite(func, call, parent)
        if not ok:
            unhandled.append(f"{fn}@{call.lineno}: {detail}")
    if unhandled:
        raise DefectStillPresent(
            "maturation transition() callsite(s) not consumed by a per-result Pending arm before the "
            f"success/dependent-work path: {sorted(unhandled)}"
        )


def test_r21_maturation_callsite_inventory_control_sees_exact_six() -> None:
    """GREEN control (unmarked): the scanner must see EXACTLY the six reviewed callsites. A callsite that
    silently disappears (scanner regression / unaccounted migration) OR a new one appearing fails here."""
    found = Counter(fn for fn, _ in _maturation_transition_callsites())
    assert found == _EXPECTED_MATURATION_CALLSITES, (
        f"maturation transition() callsite drift: found={dict(found)} "
        f"expected={dict(_EXPECTED_MATURATION_CALLSITES)} — account for every change"
    )


def _first_callsite(src: str, which: int = 0) -> tuple[ast.AST, ast.Call, dict[ast.AST, ast.AST]]:
    """Parse a one-function snippet and return (func-node, the ``which``-th ``transition(...)`` call node,
    parent-map) for the per-callsite analyzer discriminator."""
    tree = ast.parse(src)
    parent = _parent_map(tree)
    func = cast(ast.AST, tree.body[0])
    calls = [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "transition"
    ]
    return func, calls[which], parent


def test_r21_callsite_pending_arm_rule_discriminates() -> None:
    """GREEN mechanism proof (unmarked): the per-callsite analyzer ACCEPTS the correct three-way form and
    independently REJECTS each wrong shape — a pending branch on a DIFFERENT value, an unrelated
    pending_count that is not a branch on the result, a Pending check placed AFTER the success path, and
    (for a two-callsite sweep) proves handling ONE callsite leaves the OTHER independently failing."""
    # (v) the correct per-callsite three-way form -> accepted
    correct = (
        "def s():\n"
        "    result = transition()\n"
        "    if isinstance(result.value, TransitionPending):\n"
        "        deferred.append(result)\n"
        "        continue\n"
        "    if not isinstance(result, Ok):\n"
        "        failed += 1\n"
        "        continue\n"
        "    transitioned += 1\n"
    )
    func, call, parent = _first_callsite(correct)
    ok, _detail = _pending_arm_ok_for_callsite(func, call, parent)
    assert ok, "the correct per-callsite three-way form must be accepted"

    # (ii) an unrelated `pending_count` that is not a branch on the result -> not accepted
    unrelated = (
        "def s():\n"
        "    result = transition()\n"
        "    if pending_count > 0:\n"
        "        note()\n"
        "    if isinstance(result, Ok):\n"
        "        transitioned += 1\n"
    )
    func, call, parent = _first_callsite(unrelated)
    ok, _detail = _pending_arm_ok_for_callsite(func, call, parent)
    assert not ok, "an unrelated pending_count (not a branch on the result) must not be accepted"

    # (iii) a Pending branch on a DIFFERENT value (not this callsite's result) -> not accepted
    different = (
        "def s():\n"
        "    result = transition()\n"
        "    other = transition()\n"
        "    if isinstance(other.value, TransitionPending):\n"
        "        deferred.append(other)\n"
        "        continue\n"
        "    transitioned += 1\n"
    )
    func, call, parent = _first_callsite(different, which=0)  # the `result` callsite
    ok, _detail = _pending_arm_ok_for_callsite(func, call, parent)
    assert not ok, "a Pending branch on a different value must not be accepted for this callsite"

    # (iv) a Pending check placed AFTER the completed/dependent-work path -> not accepted
    after = (
        "def s():\n"
        "    result = transition()\n"
        "    if isinstance(result, Ok):\n"
        "        transitioned += 1\n"
        "    if isinstance(result.value, TransitionPending):\n"
        "        deferred.append(result)\n"
    )
    func, call, parent = _first_callsite(after)
    ok, _detail = _pending_arm_ok_for_callsite(func, call, parent)
    assert not ok, "a Pending check placed after the success path must not be accepted"

    # (i) one of two episodic callsites handled but the OTHER not -> the unhandled one still fails
    one_of_two = (
        "def s():\n"
        "    result = transition()\n"
        "    if isinstance(result.value, TransitionPending):\n"
        "        deferred.append(result)\n"
        "        continue\n"
        "    if superseded:\n"
        "        back_result = transition()\n"
        "        if not isinstance(back_result, Ok):\n"
        "            log()\n"
        "    transitioned += 1\n"
    )
    func, fwd_call, parent = _first_callsite(one_of_two, which=0)
    ok_fwd, _detail = _pending_arm_ok_for_callsite(func, fwd_call, parent)
    assert ok_fwd, "the handled forward callsite must be accepted"
    func2, back_call, parent2 = _first_callsite(one_of_two, which=1)
    ok_back, _detail = _pending_arm_ok_for_callsite(func2, back_call, parent2)
    assert not ok_back, "the unhandled back-link callsite must still fail (independently caught)"


# ---- TASK 2 discriminator: the deferred-accounting acceptance check catches each failure mode ------- #


class _DeferAccount:
    """Reference sweep accounting used only as a discriminator oracle (no src)."""

    def __init__(self) -> None:
        self.transitioned: int = 0
        self.deferred: list[tuple[Any, Any]] = []
        self.dependent_ran: int = 0
        self.retried: int = 0


def _apply_correct(
    acc: _DeferAccount, kind: str, opk: str, ev: str | None, *, has_dependent: bool
) -> None:
    if kind == "final":
        acc.transitioned += 1
        if has_dependent:
            acc.dependent_ran += 1
    elif kind == "pending":
        acc.deferred.append((opk, ev))  # retained; NOT counted, no dependent work, no retry


def _apply_counts_pending(
    acc: _DeferAccount, kind: str, opk: str, ev: str | None, *, has_dependent: bool
) -> None:
    if kind == "pending":
        acc.transitioned += 1  # WRONG: a deferral counted as a completed transition


def _apply_runs_dependent(
    acc: _DeferAccount, kind: str, opk: str, ev: str | None, *, has_dependent: bool
) -> None:
    if kind == "pending":
        acc.deferred.append((opk, ev))
        if has_dependent:
            acc.dependent_ran += 1  # WRONG: dependent work on a Pending forward


def _apply_retries(
    acc: _DeferAccount, kind: str, opk: str, ev: str | None, *, has_dependent: bool
) -> None:
    if kind == "pending":
        acc.deferred.append((opk, ev))
        acc.retried += 1  # WRONG: immediate direct retry


def _apply_drops_id(
    acc: _DeferAccount, kind: str, opk: str, ev: str | None, *, has_dependent: bool
) -> None:
    if kind == "pending":
        acc.deferred.append((opk, None))  # WRONG: event_id dropped from deferred accounting


def _deferred_ok_after_pending(acc: _DeferAccount) -> bool:
    return (
        acc.transitioned == 0
        and acc.dependent_ran == 0
        and acc.retried == 0
        and len(acc.deferred) == 1
        and all(x is not None and x != "" for x in acc.deferred[0])
    )


def test_r21_deferred_accounting_check_discriminates() -> None:
    """GREEN mechanism proof (unmarked): the deferred-accounting acceptance check passes the correct
    policy and independently REJECTS each failure mode — pending-counted-as-completed,
    dependent-work-run-on-pending, immediate-retry-on-pending, and dropped-identifier."""

    def run(apply: Callable[..., None]) -> _DeferAccount:
        acc = _DeferAccount()
        apply(acc, "pending", _R21_OPK, _R21_EVENT_ID, has_dependent=True)
        return acc

    assert _deferred_ok_after_pending(run(_apply_correct))
    assert not _deferred_ok_after_pending(run(_apply_counts_pending))
    assert not _deferred_ok_after_pending(run(_apply_runs_dependent))
    assert not _deferred_ok_after_pending(run(_apply_retries))
    assert not _deferred_ok_after_pending(run(_apply_drops_id))
    # and the correct policy still COMPLETES a Final (with its dependent work), not a false deferral
    facc = _DeferAccount()
    _apply_correct(facc, "final", _R21_OPK, _R21_EVENT_ID, has_dependent=True)
    assert facc.transitioned == 1 and facc.dependent_ran == 1 and facc.deferred == []


# ==================================================================================================== #
# P0c — production-wiring reds (topology / readiness / concurrency / settings / config-drift).          #
# tests/docs ONLY, ZERO src. Each strict-xfail raises a DEDICATED DefectStillPresent for its named       #
# missing behavior ONLY; discriminators/controls are UNMARKED and PASS today. Authoritative context:     #
# docs/Musubi/13-decisions/c6b-phase1-source-cut-plan.md §C (topology), §D (concurrency), §E (config     #
# drift), §G (settings), §H (readiness). AST/text inspection never imports the (absent) coordinator.     #
# ==================================================================================================== #

_P0C_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The Phase-1 coordinator type the composition must build in BOTH processes (§C). AST/text only.
_COORDINATOR_TYPE = "LifecycleTransitionCoordinator"
#: The single reconciler entrypoint (§C: worker-only).
_RECONCILE_NAME = "reconcile_once"
#: The provider the API injects its app-lifetime coordinator through (dependencies.py:116).
_LIFECYCLE_PROVIDER = "get_lifecycle_service"
#: The API composition surfaces that MUST NOT compose a reconciler (§C).
_API_COMPOSITION_SURFACES = ("api/bootstrap.py", "api/app.py", "api/dependencies.py")


def _parse_src(relpath: str) -> ast.Module:
    """Parse a REAL src/musubi module (no import, so the absent coordinator never trips ImportError)."""
    return ast.parse((_SRC / relpath).read_text())


def _func_named(module: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _constructs_type(scope: ast.AST, type_name: str) -> bool:
    """A Call whose callee is ``type_name`` (bare ``Name`` or ``<mod>.type_name``) in this subtree."""
    for n in ast.walk(scope):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == type_name:
                return True
            if isinstance(f, ast.Attribute) and f.attr == type_name:
                return True
    return False


def _refs_call(scope: ast.AST, name: str) -> bool:
    """A reference to ``name`` anywhere in this subtree — called (``name(...)`` / ``x.name(...)``) OR
    passed as a value (``jobs=[..., x.name]``), so a reconcile loop wired as an interval-job callable
    still counts as composed."""
    for n in ast.walk(scope):
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
        if isinstance(n, ast.Name) and n.id == name:
            return True
    return False


# ---- TASK 1: topology reds (§C — one coordinator PER PROCESS, reconcile worker-only) --------------- #


def _dep_override_value(func: ast.AST, provider: str) -> ast.AST | None:
    """The RHS assigned to ``<app>.dependency_overrides[<provider>] = RHS`` within ``func``, else None."""
    for n in ast.walk(func):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Attribute)
                    and tgt.value.attr == "dependency_overrides"
                    and isinstance(tgt.slice, ast.Name)
                    and tgt.slice.id == provider
                ):
                    return n.value
    return None


def _names_from_ctor(func: ast.AST, type_name: str) -> set[str]:
    """Local names bound (``name = Type(...)``) to a construction of ``type_name`` within ``func``."""
    out: set[str] = set()
    for n in ast.walk(func):
        if isinstance(n, ast.Assign) and _constructs_type(n.value, type_name):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
    return out


def _value_is_coordinator(func: ast.AST, rhs: ast.AST, type_name: str) -> bool:
    """Does the injected provider value resolve to a coordinator? True iff the RHS constructs it inline,
    OR references (e.g. ``lambda: coord``) a local bound to a coordinator ctor in the same function."""
    if _constructs_type(rhs, type_name):
        return True
    bound = _names_from_ctor(func, type_name)
    if not bound:
        return False
    return any(isinstance(x, ast.Name) and x.id in bound for x in ast.walk(rhs))


def _bootstrap_injects_coordinator(module: ast.AST) -> bool:
    func = _func_named(module, "bootstrap_production_app")
    if func is None:
        return False
    rhs = _dep_override_value(func, _LIFECYCLE_PROVIDER)
    if rhs is None:
        return False
    return _value_is_coordinator(func, rhs, _COORDINATOR_TYPE)


def _worker_builds_coordinator_and_reconcile(module: ast.AST) -> tuple[bool, bool]:
    func = _func_named(module, "_main_async")
    has_coord = func is not None and _constructs_type(func, _COORDINATOR_TYPE)
    has_reconcile = _refs_call(module, _RECONCILE_NAME)
    return (has_coord, has_reconcile)


def _reconcile_worker_only() -> tuple[bool, bool]:
    """(worker_composes_reconcile, api_composes_reconcile) across the REAL composition surfaces."""
    worker = _refs_call(_parse_src("lifecycle/runner.py"), _RECONCILE_NAME)
    api = any(_refs_call(_parse_src(s), _RECONCILE_NAME) for s in _API_COMPOSITION_SURFACES)
    return (worker, api)


_P0C_T1A_REASON = (
    "today bootstrap_production_app injects a bare {qdrant, embedder} dict for get_lifecycle_service "
    "(bootstrap.py:249-252) and dependencies.py:116 still raises NotImplementedError, so NO app-lifetime "
    "LifecycleTransitionCoordinator is constructed or injected; §C requires the API to build ONE "
    "coordinator and inject it via app.dependency_overrides[get_lifecycle_service]."
)
_P0C_T1B_REASON = (
    "today runner._main_async builds sink+cursors (runner.py:365-367) but constructs NO process-lifetime "
    "LifecycleTransitionCoordinator and wires NO reconcile_once loop; §C requires the worker to build ONE "
    "coordinator and run the ONLY reconcile_once (startup pass + a build_lifecycle_jobs interval job)."
)
_P0C_T1C_REASON = (
    "today reconcile_once is composed in NO process (absent from lifecycle/runner.py AND from every API "
    "composition surface), so the §C worker-ONLY-reconciler split (reconcile_once present in the worker, "
    "absent in the API) cannot yet hold; the API path must never compose a reconciler."
)


def test_p0c_bootstrap_injects_app_lifetime_coordinator() -> None:
    if not _bootstrap_injects_coordinator(_parse_src("api/bootstrap.py")):
        raise DefectStillPresent(
            f"bootstrap_production_app injects no app-lifetime {_COORDINATOR_TYPE} via "
            f"app.dependency_overrides[{_LIFECYCLE_PROVIDER}] (the injected value is a bare dict; "
            "dependencies.py:116 still raises NotImplementedError)"
        )


def test_p0c_worker_builds_coordinator_and_wires_reconcile() -> None:
    has_coord, has_reconcile = _worker_builds_coordinator_and_reconcile(
        _parse_src("lifecycle/runner.py")
    )
    if not (has_coord and has_reconcile):
        missing = []
        if not has_coord:
            missing.append(f"no {_COORDINATOR_TYPE} constructed in _main_async")
        if not has_reconcile:
            missing.append(f"no {_RECONCILE_NAME} wired in the runner")
        raise DefectStillPresent(
            "lifecycle/runner.py does not build the process-lifetime coordinator + reconcile loop: "
            + "; ".join(missing)
        )


def test_p0c_reconcile_is_worker_only() -> None:
    worker, api = _reconcile_worker_only()
    if not (worker and not api):
        raise DefectStillPresent(
            f"{_RECONCILE_NAME} worker-only split does not hold: composed in worker={worker}, composed "
            f"in API surfaces {list(_API_COMPOSITION_SURFACES)}={api}; §C requires reconcile present in "
            "the worker and ABSENT from the API composition"
        )


def test_p0c_bootstrap_injection_rule_discriminates() -> None:
    """GREEN mechanism proof: the injection check accepts a coordinator injected through
    get_lifecycle_service (bound-name OR inline) and REJECTS the current dict, a coordinator injected
    under the WRONG provider, and no injection at all."""
    correct = ast.parse(
        "def bootstrap_production_app(app, settings):\n"
        "    coord = LifecycleTransitionCoordinator(client=q, db_path=settings.lifecycle_sqlite_path)\n"
        "    app.dependency_overrides[get_lifecycle_service] = lambda: coord\n"
    )
    inline = ast.parse(
        "def bootstrap_production_app(app, settings):\n"
        "    app.dependency_overrides[get_lifecycle_service] = lambda: "
        "LifecycleTransitionCoordinator(client=q, db_path=settings.lifecycle_sqlite_path)\n"
    )
    dict_today = ast.parse(
        "def bootstrap_production_app(app, settings):\n"
        "    app.dependency_overrides[get_lifecycle_service] = lambda: {'qdrant': q}\n"
    )
    wrong_key = ast.parse(
        "def bootstrap_production_app(app, settings):\n"
        "    coord = LifecycleTransitionCoordinator(client=q, db_path=settings.lifecycle_sqlite_path)\n"
        "    app.dependency_overrides[get_qdrant_client] = lambda: coord\n"
        "    app.dependency_overrides[get_lifecycle_service] = lambda: {'qdrant': q}\n"
    )
    assert _bootstrap_injects_coordinator(correct)
    assert _bootstrap_injects_coordinator(inline)
    assert not _bootstrap_injects_coordinator(dict_today)
    assert not _bootstrap_injects_coordinator(wrong_key)


def test_p0c_worker_reconcile_rule_discriminates() -> None:
    """GREEN mechanism proof: the worker check distinguishes coordinator+reconcile from each partial and
    from the current absence."""
    correct = ast.parse(
        "async def _main_async():\n"
        "    coord = LifecycleTransitionCoordinator(client=q, db_path=settings.lifecycle_sqlite_path)\n"
        "    await coord.reconcile_once(limit=100)\n"
    )
    coord_only = ast.parse(
        "async def _main_async():\n    coord = LifecycleTransitionCoordinator(client=q, db_path=p)\n"
    )
    reconcile_only = ast.parse("async def _main_async():\n    await reconcile_once(limit=100)\n")
    today = ast.parse(
        "async def _main_async():\n    sink = LifecycleEventSink(db_path=settings.lifecycle_sqlite_path)\n"
    )
    assert _worker_builds_coordinator_and_reconcile(correct) == (True, True)
    assert _worker_builds_coordinator_and_reconcile(coord_only) == (True, False)
    assert _worker_builds_coordinator_and_reconcile(reconcile_only) == (False, True)
    assert _worker_builds_coordinator_and_reconcile(today) == (False, False)


def test_p0c_worker_only_reconcile_rule_discriminates() -> None:
    """GREEN mechanism proof: the worker-only invariant accepts reconcile-in-worker/absent-in-API and
    REJECTS both the current absence and a reconciler leaked into the API composition."""
    worker_mod = ast.parse("async def _main_async():\n    await coord.reconcile_once()\n")
    worker_absent = ast.parse(
        "async def _main_async():\n    sink = LifecycleEventSink(db_path=p)\n"
    )
    api_clean = ast.parse(
        "def bootstrap_production_app(app, s):\n    app.dependency_overrides[x] = 1\n"
    )
    api_dirty = ast.parse("def bootstrap_production_app(app, s):\n    coord.reconcile_once()\n")

    def holds(worker: ast.AST, api: ast.AST) -> bool:
        return _refs_call(worker, _RECONCILE_NAME) and not _refs_call(api, _RECONCILE_NAME)

    assert holds(worker_mod, api_clean)
    assert not holds(worker_absent, api_clean)
    assert not holds(worker_mod, api_dirty)


# ---- TASK 2: readiness red (§H — worker healthcheck must consume a readiness signal, not /metrics) - #

#: Pinned FUTURE readiness signal (§H). A gauge on the worker metrics port set to 1 ONLY when the
#: coordinator's shared SQLite/outbox schema is open AND reconcile_once can safely participate. The
#: deploy healthcheck must CONSUME this (or a dedicated readiness endpoint) — not merely probe /metrics
#: for HTTP 200 (liveness). "A gauge is not a readiness gate until deployment consumes it."
_READINESS_GAUGE = "musubi_lifecycle_coordinator_ready"
_READINESS_PATHS = ("/readyz", "/healthz", "/readiness")


def _worker_healthcheck_test(compose_text: str) -> object:
    """The lifecycle-worker healthcheck ``test:`` from the REAL ansible compose template. Jinja
    expressions are neutralized so PyYAML can load; the worker healthcheck itself is jinja-free."""
    doc = yaml.safe_load(re.sub(r"{{.*?}}", "JINJA", compose_text))
    return doc["services"]["lifecycle-worker"]["healthcheck"]["test"]


def _healthcheck_consumes_readiness(test_cmd: object) -> bool:
    """Does the healthcheck consume a READINESS signal (the pinned gauge OR a dedicated readiness
    endpoint) rather than only probing /metrics for liveness?"""
    text = " ".join(str(x) for x in test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    if _READINESS_GAUGE in text:
        return True
    return any(p in text for p in _READINESS_PATHS)


_P0C_T2_REASON = (
    "today the lifecycle-worker healthcheck (docker-compose.yml.j2:57-62) probes only "
    "http://localhost:8101/metrics for HTTP 200 (Prometheus liveness) and consumes NO readiness signal "
    "(the pinned musubi_lifecycle_coordinator_ready gauge or a /readyz-style endpoint) proving the "
    "coordinator's storage/schema is open + reconcile can safely participate; §H holds the release until "
    "the production healthcheck consumes readiness."
)


def test_p0c_worker_healthcheck_consumes_readiness_signal() -> None:
    template = (_P0C_REPO_ROOT / "deploy/ansible/templates/docker-compose.yml.j2").read_text()
    test_cmd = _worker_healthcheck_test(template)
    if not _healthcheck_consumes_readiness(test_cmd):
        raise DefectStillPresent(
            "the lifecycle-worker healthcheck consumes no readiness signal "
            f"({_READINESS_GAUGE} gauge or a {list(_READINESS_PATHS)} endpoint); it only probes "
            f"/metrics for HTTP 200. test={test_cmd!r}"
        )


def test_p0c_readiness_probe_rule_discriminates() -> None:
    """GREEN mechanism proof: the readiness rule rejects the current /metrics-200 liveness probe and
    accepts a probe that consumes the pinned readiness gauge OR a readiness endpoint — and it classifies
    the REAL template's current healthcheck as the liveness-only case."""
    metrics_only = [
        "CMD",
        "python",
        "-c",
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("
        "'http://localhost:8101/metrics', timeout=2).status == 200 else 1)",
    ]
    gauge_probe = [
        "CMD",
        "python",
        "-c",
        "import urllib.request,sys; b=urllib.request.urlopen('http://localhost:8101/metrics')"
        f".read().decode(); sys.exit(0 if '{_READINESS_GAUGE} 1' in b else 1)",
    ]
    readyz_probe = [
        "CMD",
        "python",
        "-c",
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("
        "'http://localhost:8101/readyz', timeout=2).status == 200 else 1)",
    ]
    assert not _healthcheck_consumes_readiness(metrics_only)
    assert _healthcheck_consumes_readiness(gauge_probe)
    assert _healthcheck_consumes_readiness(readyz_probe)
    real = _worker_healthcheck_test(
        (_P0C_REPO_ROOT / "deploy/ansible/templates/docker-compose.yml.j2").read_text()
    )
    assert _healthcheck_consumes_readiness(real)


# ---- TASK 3: concurrent shared-file test (§D — WAL + busy_timeout + cross-process schema init) ----- #

_T3_PROCS = 4
_T3_WRITES = 25


def _connection_policy_ok(journal_mode: str, busy_timeout: int) -> bool:
    """The shared-file connection policy the shared file REQUIRES (§D): WAL + a positive busy_timeout."""
    return journal_mode.lower() == "wal" and busy_timeout > 0


def _t3_child_source(*, db_path: Path, barrier_dir: Path, tag: str, n_writes: int) -> str:
    """A child that opens the REAL sink + BOTH cursors on ONE shared file (concurrent CREATE TABLE IF NOT
    EXISTS), rendezvous-waits so every child writes at once, then does n_writes real cursor writes.
    ``sqlite3.OperationalError`` ('database is locked' — the busy_timeout==0 symptom) is caught and
    recorded via a marker file; the child STILL exits 0 so the PARENT owns the one dedicated policy
    assertion and no unrelated exception can leak into the test process."""
    return (
        "import os, sys, time, warnings, sqlite3\n"
        "from pathlib import Path\n"
        "from musubi.lifecycle.events import LifecycleEventSink\n"
        "from musubi.lifecycle.maturation import MaturationCursor\n"
        "from musubi.lifecycle.synthesis import SynthesisCursor\n"
        f"_bd = {str(barrier_dir)!r}\n"
        f"_db = Path({str(db_path)!r})\n"
        f"_tag = {tag!r}\n"
        f"_n = {n_writes}\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    sink = LifecycleEventSink(db_path=_db)\n"
        "    mat = MaturationCursor(db_path=_db)\n"
        "    syn = SynthesisCursor(db_path=_db)\n"
        "open(os.path.join(_bd, 'ready.' + str(os.getpid())), 'w').close()\n"
        "for _ in range(1000):\n"
        f"    if len([f for f in os.listdir(_bd) if f.startswith('ready.')]) >= {_T3_PROCS}: break\n"
        "    time.sleep(0.01)\n"
        "try:\n"
        "    for i in range(_n):\n"
        "        mat.set(_tag + '-' + str(i), float(i))\n"
        "        syn.set(_tag, float(i))\n"
        "except sqlite3.OperationalError:\n"
        "    open(os.path.join(_bd, 'locked.' + str(os.getpid())), 'w').close()\n"
        "sink.close()\n"
        "sys.exit(0)\n"
    )


def test_p0c_shared_file_requires_wal_and_busy_timeout(tmp_path: Path) -> None:
    base = tmp_path / "t3"
    base.mkdir()
    db_path = base / "lifecycle.sqlite"
    barrier = base / "barrier"
    barrier.mkdir()
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _t3_child_source(
                    db_path=db_path, barrier_dir=barrier, tag=f"p0c{i}", n_writes=_T3_WRITES
                ),
            ]
        )
        for i in range(_T3_PROCS)
    ]
    codes = [p.wait(timeout=90) for p in procs]
    # Cross-process CREATE TABLE IF NOT EXISTS must not corrupt/deadlock: every child exits cleanly and
    # the shared schema is intact afterward (GREEN baseline — the DEFECT is the missing policy, below).
    if any(c != 0 for c in codes):
        raise DefectStillPresent(
            "concurrent cross-process openers of the shared lifecycle file did not all exit cleanly: "
            f"exit codes {codes} (schema init / write under the bare connection is not cross-process safe)"
        )
    for table in ("lifecycle_events", "maturation_cursor", "synthesis_family_cursor"):
        if not _table_exists(db_path, table):
            raise DefectStillPresent(
                f"shared schema missing table {table!r} after concurrent multi-process init"
            )
    # DEDICATED policy probe on the REAL sink (the worker's connection owner). WAL is a persistent
    # database-level mode; the bare connection reports the default rollback journal ('delete'). NOTE: the
    # sink's busy_timeout is 5000ms today — NOT via PRAGMA (events.py sets none) but via CPython's
    # sqlite3.connect default timeout=5.0s; the observable gap the shared file still fails on is WAL.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sink = LifecycleEventSink(db_path=db_path)
    try:
        journal_mode = str(sink._conn.execute("PRAGMA journal_mode").fetchone()[0])
        busy_timeout = int(sink._conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        sink.close()
    if not _connection_policy_ok(journal_mode, busy_timeout):
        violations = []
        if journal_mode.lower() != "wal":
            violations.append(f"journal_mode={journal_mode!r} (need 'wal')")
        if busy_timeout <= 0:
            violations.append(f"busy_timeout={busy_timeout} (need > 0)")
        raise DefectStillPresent(
            "the shared-file LifecycleEventSink connection lacks the cross-process write policy the shared "
            f"file requires ({', '.join(violations)}); {_T3_PROCS} real processes shared {db_path.name}. "
            "events.py:19 claims WAL but :79-84 sets no PRAGMA journal_mode=WAL."
        )


def test_p0c_connection_policy_rule_discriminates() -> None:
    """GREEN mechanism proof: the policy check accepts WAL+busy_timeout and REJECTS each partial and the
    current bare-connection shape."""
    assert _connection_policy_ok("wal", 5000)
    assert _connection_policy_ok("WAL", 1)
    assert not _connection_policy_ok("delete", 0)  # today's bare connection
    assert not _connection_policy_ok("wal", 0)  # WAL but no busy_timeout
    assert not _connection_policy_ok("delete", 5000)  # busy_timeout but no WAL
    assert not _connection_policy_ok("memory", 5000)


# ---- TASK 4: settings-validation reds (§G — validate finite/positive/bounded) --------------------- #


def _validate_positive_float(name: str, v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(f"{name} must be a real number (not bool), got {type(v).__name__}")
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{name} must be positive and finite, got {v}")
    return float(v)


def _validate_positive_int(name: str, v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"{name} must be an int (not bool), got {type(v).__name__}")
    if v <= 0:
        raise ValueError(f"{name} must be a positive int, got {v}")
    return v


def _validate_nonneg_int(name: str, v: object) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"{name} must be an int (not bool), got {type(v).__name__}")
    if v < 0:
        raise ValueError(f"{name} must be a non-negative int, got {v}")
    return v


def _validate_backoff_pair(base: object, mx: object) -> tuple[float, float]:
    b = _validate_positive_float("backoff_base_s", base)
    m = _validate_positive_float("backoff_max_s", mx)
    if m < b:
        raise ValueError(f"backoff_max_s ({m}) must be >= backoff_base_s ({b})")
    return b, m


#: Every NEW setting the composition will consume (§G). Names are the fields the reds require on
#: ``Settings``; the constraint column is the finite/positive/bounded rule each must validate.
_P0C_NEW_SETTINGS: list[tuple[str, str]] = [
    ("lifecycle_pending_cap", "int>0"),
    ("lifecycle_lease_ttl_s", "float>0"),
    ("lifecycle_reconcile_interval_s", "int>0"),
    ("lifecycle_backoff_base_s", "float>0"),
    ("lifecycle_backoff_max_s", "float>0,>=base"),
    ("lifecycle_sqlite_busy_timeout_ms", "int>=0"),
    ("lifecycle_cleanup_retention_s", "int>0"),
    ("lifecycle_cleanup_batch", "int>0"),
    ("lifecycle_readiness_max_reconcile_failures", "int>0"),
]

_P0C_T4_REASON = (
    "today Settings carries only lifecycle_sqlite_path (:102) + lifecycle_metrics_port (:116); NONE of "
    "the composition-consumed lifecycle settings (pending cap, lease TTL, reconcile cadence, backoff "
    "base/max, sqlite busy_timeout, cleanup retention/batch, readiness/reconcile-failure thresholds) "
    "exist, so none can be resolved OR validated finite/positive/bounded (§G)."
)


@pytest.mark.parametrize(
    ("field", "constraint"),
    [
        pytest.param(
            _field,
            _constraint,
            marks=()
            if _field
            in (
                "lifecycle_sqlite_busy_timeout_ms",
                "lifecycle_pending_cap",
                "lifecycle_lease_ttl_s",
                "lifecycle_reconcile_interval_s",
                "lifecycle_backoff_base_s",
                "lifecycle_backoff_max_s",
                "lifecycle_cleanup_retention_s",
                "lifecycle_cleanup_batch",
                "lifecycle_readiness_max_reconcile_failures",
            )
            else (
                pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_P0C_T4_REASON),
            ),
            id=_field,
        )
        for _field, _constraint in _P0C_NEW_SETTINGS
    ],
)
def test_p0c_new_lifecycle_setting_exists_and_validates(field: str, constraint: str) -> None:
    if field not in Settings.model_fields:
        raise DefectStillPresent(
            f"Settings has no {field!r} field (constraint {constraint}); §G requires the composition-"
            "consumed lifecycle settings to exist on Settings and validate finite/positive/bounded."
        )


def _p0c_same_active_storage_path() -> tuple[str | None, str | None]:
    """(api coordinator db_path expr, worker coordinator db_path expr) from the REAL composition."""

    def db_path_in(relpath: str, func_name: str) -> str | None:
        func = _func_named(_parse_src(relpath), func_name)
        return _coordinator_db_path_expr(func, _COORDINATOR_TYPE) if func is not None else None

    return (
        db_path_in("api/bootstrap.py", "bootstrap_production_app"),
        db_path_in("lifecycle/runner.py", "_main_async"),
    )


_P0C_T4PATH_REASON = (
    "today the API composition builds no coordinator at all (bootstrap injects a bare dict), so API and "
    "worker cannot be proven to resolve the SAME active-storage path; §G requires both to build the "
    "coordinator from settings.lifecycle_sqlite_path."
)


def test_p0c_api_and_worker_resolve_same_active_storage_path() -> None:
    api, worker = _p0c_same_active_storage_path()
    if api is None or worker is None or api != worker:
        raise DefectStillPresent(
            "API and worker do not resolve the SAME active-storage path from one setting: "
            f"api coordinator db_path={api!r}, worker coordinator db_path={worker!r} "
            "(§G: both must build the coordinator from settings.lifecycle_sqlite_path)"
        )


def test_p0c_settings_validators_discriminate() -> None:
    """GREEN mechanism proof: each reference validator accepts a good value and REJECTS <=0 / non-finite /
    bool / wrong-type / max<base — the finite/positive/bounded shape the settings must enforce."""
    assert _validate_positive_int("cap", 5) == 5
    for bad_i in (0, -1, True, 1.5, "x"):
        with pytest.raises((TypeError, ValueError)):
            _validate_positive_int("cap", bad_i)
    assert _validate_positive_float("ttl", 2.5) == 2.5
    for bad_f in (0, -1.0, True, math.inf, math.nan, "x"):
        with pytest.raises((TypeError, ValueError)):
            _validate_positive_float("ttl", bad_f)
    assert _validate_nonneg_int("busy_timeout", 0) == 0
    for bad_n in (-1, True, 2.0, "x"):
        with pytest.raises((TypeError, ValueError)):
            _validate_nonneg_int("busy_timeout", bad_n)
    assert _validate_backoff_pair(0.5, 30.0) == (0.5, 30.0)
    with pytest.raises(ValueError):
        _validate_backoff_pair(30.0, 0.5)  # max < base
    with pytest.raises((TypeError, ValueError)):
        _validate_backoff_pair(0.0, 30.0)  # base <= 0
    # parity with the already-committed R14/R16 config validators
    assert _validate_pending_cap(3) == 3
    assert _validate_lease_ttl(1.0) == 1.0


def test_p0c_same_active_storage_rule_discriminates() -> None:
    """GREEN mechanism proof: the same-path check accepts two coordinators built from the same setting and
    REJECTS a different setting or an absent API coordinator."""
    same_a = cast(
        ast.AST,
        ast.parse(
            "def f():\n    c = LifecycleTransitionCoordinator(client=q, "
            "db_path=settings.lifecycle_sqlite_path)\n"
        ).body[0],
    )
    same_b = cast(
        ast.AST,
        ast.parse(
            "def g():\n    c = LifecycleTransitionCoordinator(client=q, "
            "db_path=settings.lifecycle_sqlite_path)\n"
        ).body[0],
    )
    diff_b = cast(
        ast.AST,
        ast.parse(
            "def g():\n    c = LifecycleTransitionCoordinator(client=q, db_path=settings.other_path)\n"
        ).body[0],
    )
    none_a = cast(
        ast.AST,
        ast.parse("def f():\n    app.overrides[x] = lambda: {'qdrant': q}\n").body[0],
    )

    def same(a: ast.AST, b: ast.AST) -> bool:
        pa = _coordinator_db_path_expr(a, _COORDINATOR_TYPE)
        pb = _coordinator_db_path_expr(b, _COORDINATOR_TYPE)
        return pa is not None and pb is not None and pa == pb

    assert same(same_a, same_b)
    assert not same(same_a, diff_b)
    assert not same(none_a, same_b)
    assert _coordinator_db_path_expr(same_a, _COORDINATOR_TYPE) == "settings.lifecycle_sqlite_path"


def _dotted(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted(expr.value)
        return f"{base}.{expr.attr}" if base else expr.attr
    return None


def _coordinator_db_path_expr(func: ast.AST, type_name: str) -> str | None:
    """The dotted source of the ``db_path=`` kwarg on a ``type_name(...)`` construction in ``func`` (e.g.
    'settings.lifecycle_sqlite_path'); None if no such coordinator is constructed."""
    for n in ast.walk(func):
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == type_name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == type_name)
        ):
            for kw in n.keywords:
                if kw.arg == "db_path":
                    return _dotted(kw.value)
    return None


# ---- TASK 5: config-drift NAMED BLOCKER (§E — ansible-dir vs root-compose-file) ------------------- #

#: Documented FLIP mechanism (§E): a deployment surface may be excluded from the parity set ONLY by
#: naming it here (with a rationale + this appendix pointer, e.g. proven non-production / out-of-scope).
#: Reconciling the surfaces to ONE family, OR marking every dissenter out-of-scope, flips the red green.
_OUT_OF_SCOPE_STORAGE_SURFACES: frozenset[str] = frozenset()


def _env_lifecycle_path(text: str) -> str | None:
    m = re.search(r"^LIFECYCLE_SQLITE_PATH=(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _compose_lifecycle_host_path(text: str) -> str | None:
    """The host side of the lifecycle bind mount (``<host>:<container>``) in a compose/j2 file."""
    m = re.search(r"(/var/lib/musubi/lifecycle[\w./-]*)\s*:", text)
    return m.group(1) if m else None


def _first_lifecycle_sqlite(text: str) -> str | None:
    m = re.search(r"(/var/lib/musubi/lifecycle[\w./-]*\.sqlite)", text)
    return m.group(1) if m else None


def _lifecycle_storage_surfaces() -> dict[str, str | None]:
    r = _P0C_REPO_ROOT
    return {
        "ansible-compose": _compose_lifecycle_host_path(
            (r / "deploy/ansible/templates/docker-compose.yml.j2").read_text()
        ),
        "ansible-env-production": _env_lifecycle_path(
            (r / "deploy/ansible/templates/env.production.j2").read_text()
        ),
        "ansible-restore": _first_lifecycle_sqlite((r / "deploy/backup/restore.yml").read_text()),
        "root-compose": _compose_lifecycle_host_path((r / "docker-compose.yml").read_text()),
        "env-example": _env_lifecycle_path((r / ".env.example").read_text()),
        "docker-env-production-example": _env_lifecycle_path(
            (r / "deploy/docker/.env.production.example").read_text()
        ),
        "backup": _first_lifecycle_sqlite((r / "deploy/backup/backup.yml").read_text()),
    }


def _storage_family(path: str) -> str:
    """Classify a lifecycle storage path into its active-storage FAMILY (directory-variant vs file-
    variant). The two families are what the deployment surfaces disagree on today (§E)."""
    p = path.rstrip("/")
    if p == "/var/lib/musubi/lifecycle" or p.startswith("/var/lib/musubi/lifecycle/"):
        return "DIR:/var/lib/musubi/lifecycle/work.sqlite"
    if p == "/var/lib/musubi/lifecycle-work.sqlite":
        return "FILE:/var/lib/musubi/lifecycle-work.sqlite"
    return "OTHER:" + p


def _storage_families(
    surfaces: Mapping[str, str | None],
    out_of_scope: frozenset[str] = _OUT_OF_SCOPE_STORAGE_SURFACES,
) -> dict[str, str]:
    return {
        name: _storage_family(path)
        for name, path in surfaces.items()
        if path is not None and name not in out_of_scope
    }


def test_p0c_deployment_active_storage_parity() -> None:
    families = _storage_families(_lifecycle_storage_surfaces())
    distinct = set(families.values())
    if len(distinct) > 1:
        by_family: dict[str, list[str]] = {}
        for name, fam in families.items():
            by_family.setdefault(fam, []).append(name)
        raise DefectStillPresent(
            f"deployment surfaces DISAGREE on the lifecycle active-storage unit ({len(distinct)} "
            "families): "
            + "; ".join(f"{fam} <- {sorted(names)}" for fam, names in sorted(by_family.items()))
            + ". §E BLOCKER: reconcile the surfaces OR mark dissenters out-of-scope "
            "(_OUT_OF_SCOPE_STORAGE_SURFACES) before coordinator source lands."
        )


def test_p0c_config_surfaces_all_resolve() -> None:
    """CONTROL (unmarked): every expected deployment surface resolves to a lifecycle storage path (so a
    broken extractor fails loudly GREEN-side, not as a mysterious xfail error). After the §E config-drift
    resolution every supported surface is reconciled to the single LOCKED DIR family; a surface regressing
    to the retired FILE re-introduces a second family and fails this control loudly."""
    surfaces = _lifecycle_storage_surfaces()
    unresolved = sorted(n for n, p in surfaces.items() if p is None)
    assert not unresolved, f"config-drift extractor failed to resolve surfaces: {unresolved}"
    assert set(_storage_families(surfaces).values()) == {
        "DIR:/var/lib/musubi/lifecycle/work.sqlite",
    }


def test_p0c_active_storage_parity_rule_discriminates() -> None:
    """GREEN mechanism proof: the parity check passes agreeing surfaces, CATCHES a directory-vs-file
    mismatch, and honors both flip levers — marking a dissenter out-of-scope, and reconciling to one
    family — collapse the parity set to a single family."""
    agree = {"a": "/var/lib/musubi/lifecycle", "b": "/var/lib/musubi/lifecycle/work.sqlite"}
    disagree = {
        "a": "/var/lib/musubi/lifecycle/work.sqlite",
        "b": "/var/lib/musubi/lifecycle-work.sqlite",
    }
    assert len(set(_storage_families(agree).values())) == 1
    assert len(set(_storage_families(disagree).values())) == 2
    assert len(set(_storage_families(disagree, out_of_scope=frozenset({"b"})).values())) == 1
    reconciled = {
        "a": "/var/lib/musubi/lifecycle-work.sqlite",
        "b": "/var/lib/musubi/lifecycle-work.sqlite",
    }
    assert len(set(_storage_families(reconciled).values())) == 1


# ==================================================================================================== #
# P0c STORAGE-PARITY RED FAMILY (§E extension) — the DIR storage family is LOCKED:                      #
#   canonical active-storage unit  = the DIRECTORY /var/lib/musubi/lifecycle bind-mounted, DB           #
#                                     /var/lib/musubi/lifecycle/work.sqlite.                            #
#   retired (to be aligned away)    = the bare FILE /var/lib/musubi/lifecycle-work.sqlite.              #
# TASK 1 = one strict-xfail per DRIFT surface (RED today, flips green when the surface aligns to DIR).  #
# TASK 2 = unmarked preserve-green CONTROLS on the DIR ANCHOR surfaces (fail loudly on a FILE regress). #
# TASK 3 = migration CONTRACT spec (reference-candidate red-proof) + an "unbuilt" strict-xfail. The     #
#          migration is DOWNSTREAM + R20-gated: these tests encode its CONTRACT only, never execute it. #
# ==================================================================================================== #

#: The one locked active-storage family every surface must resolve to.
_CANONICAL_DIR_FAMILY = "DIR:/var/lib/musubi/lifecycle/work.sqlite"
#: The canonical DB FILE every DB-bearing surface must name EXACTLY (env / backup source / runbook restore
#: destination / restore target). Not "some path under the DIR" — a wrong child is a different DB.
_CANONICAL_DIR_DB = "/var/lib/musubi/lifecycle/work.sqlite"
#: The canonical DIRECTORY a compose host MOUNT must name EXACTLY (the bind-mounted unit shared by core +
#: worker). Not "some path under it" — a wrong child is a different mount.
_CANONICAL_DIR_MOUNT = "/var/lib/musubi/lifecycle"
_RETIRED_FILE_DB = "/var/lib/musubi/lifecycle-work.sqlite"


def _resolves_canonical_dir_db(path: str | None) -> bool:
    """True iff a DB-bearing surface names EXACTLY the canonical DIR DB file
    ``/var/lib/musubi/lifecycle/work.sqlite`` — NOT merely some path under ``/var/lib/musubi/lifecycle``.
    A wrong child (e.g. ``…/lifecycle/wrong.sqlite``) is a DIFFERENT database and MUST be rejected: a
    surface that named it would silently point the coordinator, backup, or restore at the wrong file while
    a family-membership check waved it through. LITERAL equality (Yua ruling): a trailing slash makes the
    string an invalid DB filename, so `.../work.sqlite/` is NOT canonical (no rstrip normalization)."""
    return path == _CANONICAL_DIR_DB


def _resolves_canonical_mount(path: str | None) -> bool:
    """True iff a compose host MOUNT names EXACTLY the canonical DIRECTORY ``/var/lib/musubi/lifecycle`` —
    NOT a wrong child (e.g. ``…/lifecycle/sub``), which would bind-mount a different subtree and break the
    shared-unit invariant while a family-membership check waved it through. LITERAL equality (Yua ruling):
    the mount must be EXACTLY `/var/lib/musubi/lifecycle` with no trailing slash (no rstrip)."""
    return path == _CANONICAL_DIR_MOUNT


def _has_lifecycle_worker_service(text: str) -> bool:
    """True iff a compose/j2 file declares a top-level ``lifecycle-worker`` service key."""
    return re.search(r"(?m)^\s{1,4}lifecycle-worker:\s*$", text) is not None


def _all_lifecycle_host_mounts(text: str) -> list[str]:
    """Every host side of a ``/var/lib/musubi/lifecycle...:<container>`` bind mount in a compose/j2 file."""
    return re.findall(r"-\s*(/var/lib/musubi/lifecycle[\w./-]*)\s*:/var/lib/musubi/lifecycle", text)


def _runbook_restore_dest(text: str) -> str | None:
    """The destination the manual-recovery runbook restores the DIR snapshot ``$SNAP/sqlite/work.sqlite``
    INTO (`sudo cp -a "$SNAP/sqlite/work.sqlite" <dest>`)."""
    m = re.search(r'cp -a\s+"\$SNAP/sqlite/work\.sqlite"\s+(\S+)', text)
    return m.group(1) if m else None


_LIFECYCLE_BACKUP_TASK_NAME = "Back up sqlite lifecycle ledger"


def _sqlite_backup_command_src(playbook_text: str) -> str | None:
    """The SOURCE DB path of the sqlite ``.backup`` command in the task NAMED EXACTLY
    ``Back up sqlite lifecycle ledger`` (Yua ruling) — bound to THAT task's name, NOT the first ``.backup``
    anywhere. An unrelated canonical ``.backup`` earlier in the file must not mask this task reading the
    retired FILE. Parsed from the actual command via the YAML task structure. Fails CLOSED (None) on zero,
    duplicate, or malformed named lifecycle-backup tasks (each → not-canonical → the drift red stays RED)."""
    named = [t for t in _yaml_tasks(playbook_text) if t.get("name") == _LIFECYCLE_BACKUP_TASK_NAME]
    if len(named) != 1:
        return None  # zero or duplicate named lifecycle-backup task -> fail closed
    cmd = named[0].get("ansible.builtin.command") or named[0].get("command")
    cmd_str = cmd.get("cmd") if isinstance(cmd, dict) else cmd
    if not isinstance(cmd_str, str):
        return None  # malformed named task -> fail closed
    m = re.search(r'sqlite3\s+(\S+)\s+"?\.backup\b', cmd_str)
    return m.group(1) if m else None


def _readme_stores_section_lines(text: str) -> list[str]:
    """The lines under the README's ``## Stores`` heading, up to the next heading (any level). Code fences
    are tracked so a ``#`` inside a fenced block cannot be misread as a heading and prematurely end it."""
    out: list[str] = []
    in_section = False
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if in_section:
                out.append(line)
            continue
        if not in_fence and re.match(r"^#{1,6}\s", line):
            # EXACT heading text (Yua round-3 ruling): the normalized heading must be precisely "Stores"
            # (case-insensitive policy) — a prefix/suffix lookalike ("Stores history", "Stores-old") is NOT
            # the ## Stores section.
            heading = re.sub(r"^#{1,6}\s+", "", line.strip()).strip()
            in_section = heading.casefold() == "stores"
            continue
        if in_section:
            out.append(line)
    return out


def _readme_operational_storage_line(text: str) -> str | None:
    """The SINGLE operational lifecycle-storage bullet in the README's ``## Stores`` section (Yua ruling):
    a bullet naming the lifecycle sqlite copy. ONLY the Stores section binds the active-storage unit —
    historical/migration prose or any other section is NOT inspected, so a History line naming the DIR
    elsewhere cannot green a Stores bullet that still names the retired FILE. Fails CLOSED (None) on zero,
    duplicate, or ambiguous Stores bullets naming a lifecycle db."""
    bullets = [
        ln
        for ln in _readme_stores_section_lines(text)
        if re.match(r"^\s*[-*]\s", ln) and "work.sqlite" in ln
    ]
    return bullets[0] if len(bullets) == 1 else None


def _readme_resolves_dir(text: str) -> bool:
    """True iff the backup README's OPERATIONAL storage statement (the hourly-copy 'Stores' bullet) names
    the canonical DIR DB ``lifecycle/work.sqlite`` and NOT the bare retired FILE ``lifecycle-work.sqlite``.
    A historical/migration mention of the retired FILE ELSEWHERE in the README does NOT trip the red — only
    the operational line is inspected, so we ban the FILE in the current-storage statement, not the filename
    anywhere in the document."""
    line = _readme_operational_storage_line(text)
    if line is None:
        return False
    names_file = re.search(r"lifecycle-work\.sqlite", line) is not None
    names_dir = re.search(r"lifecycle/work\.sqlite", line) is not None
    return names_dir and not names_file


# ---- TASK 1: drift reds (one strict-xfail per drift surface; DEDICATED DefectStillPresent each) ----- #


def test_p0c_drift_root_compose_dir_mount_and_worker() -> None:
    text = (_P0C_REPO_ROOT / "docker-compose.yml").read_text()
    mount = _compose_lifecycle_host_path(text)
    worker = _has_lifecycle_worker_service(text)
    if not (_resolves_canonical_mount(mount) and worker):
        raise DefectStillPresent(
            "root compose docker-compose.yml does not resolve the canonical DIR storage: lifecycle host "
            f"mount={mount!r} (needs EXACTLY {_CANONICAL_DIR_MOUNT!r}), "
            f"lifecycle-worker service present={worker}. §E: root compose must mount the DIR and run a "
            "lifecycle-worker before coordinator source lands."
        )


def test_p0c_drift_env_example() -> None:
    path = _env_lifecycle_path((_P0C_REPO_ROOT / ".env.example").read_text())
    if not _resolves_canonical_dir_db(path):
        raise DefectStillPresent(
            f".env.example LIFECYCLE_SQLITE_PATH={path!r} does not name EXACTLY the canonical DIR DB "
            f"{_CANONICAL_DIR_DB!r} (it names the retired FILE {_RETIRED_FILE_DB!r})."
        )


def test_p0c_drift_docker_env_production_example() -> None:
    path = _env_lifecycle_path(
        (_P0C_REPO_ROOT / "deploy/docker/.env.production.example").read_text()
    )
    if not _resolves_canonical_dir_db(path):
        raise DefectStillPresent(
            f"deploy/docker/.env.production.example LIFECYCLE_SQLITE_PATH={path!r} does not name EXACTLY "
            f"the canonical DIR DB {_CANONICAL_DIR_DB!r} (it names the retired FILE {_RETIRED_FILE_DB!r})."
        )


def test_p0c_drift_backup_yml() -> None:
    # Bind to the SOURCE of the actual sqlite `.backup` command, not the first lifecycle path anywhere in
    # the file, so a comment/migration reference to the DIR cannot mask a command that still reads the FILE.
    path = _sqlite_backup_command_src((_P0C_REPO_ROOT / "deploy/backup/backup.yml").read_text())
    if not _resolves_canonical_dir_db(path):
        raise DefectStillPresent(
            f"deploy/backup/backup.yml's sqlite `.backup` command reads {path!r}, not the canonical DIR DB "
            f"{_CANONICAL_DIR_DB!r} (it reads the retired FILE {_RETIRED_FILE_DB!r}); the offsite backup "
            "must source the same DIR DB the live scheduler + restore.yml use."
        )


def test_p0c_drift_manual_recovery_runbook() -> None:
    dest = _runbook_restore_dest(
        (_P0C_REPO_ROOT / "deploy/runbooks/manual-recovery.md").read_text()
    )
    if not _resolves_canonical_dir_db(dest):
        raise DefectStillPresent(
            f"deploy/runbooks/manual-recovery.md restores the DIR snapshot $SNAP/sqlite/work.sqlite INTO "
            f"{dest!r}, not the canonical DIR DB {_CANONICAL_DIR_DB!r} (it targets the retired FILE "
            f"{_RETIRED_FILE_DB!r})."
        )


def test_p0c_drift_backup_readme() -> None:
    text = (_P0C_REPO_ROOT / "deploy/backup/README.md").read_text()
    if not _readme_resolves_dir(text):
        raise DefectStillPresent(
            "deploy/backup/README.md still names the retired FILE 'lifecycle-work.sqlite' for the hourly "
            "SQLite copy instead of the canonical DIR DB 'lifecycle/work.sqlite'."
        )


def test_p0c_drift_parsers_discriminate() -> None:
    """GREEN mechanism proof: every TASK-1 drift parser distinguishes the LOCKED DIR unit from the retired
    FILE unit — AND, crucially, rejects a WRONG CHILD under the canonical parent (a DB-bearing surface must
    name EXACTLY ``…/lifecycle/work.sqlite``, a mount EXACTLY ``…/lifecycle``). A family-membership check
    would wave ``…/lifecycle/wrong.sqlite`` / ``…/lifecycle/sub`` through and flip a red falsely green;
    exact-equality predicates catch it. Without this, a mis-parse could make a drift red pass (or an aligned
    surface still fail) silently."""
    wrong_child_db = "/var/lib/musubi/lifecycle/wrong.sqlite"
    wrong_child_mount = "/var/lib/musubi/lifecycle/sub"

    # env parser (shared by .env.example + docker/.env.production.example) — DB-bearing, EXACT DB.
    assert _resolves_canonical_dir_db(
        _env_lifecycle_path("LIFECYCLE_SQLITE_PATH=/var/lib/musubi/lifecycle/work.sqlite")
    )
    assert not _resolves_canonical_dir_db(
        _env_lifecycle_path("LIFECYCLE_SQLITE_PATH=/var/lib/musubi/lifecycle-work.sqlite")
    )
    assert not _resolves_canonical_dir_db(  # wrong child under the DIR is a DIFFERENT db — REJECTED
        _env_lifecycle_path(f"LIFECYCLE_SQLITE_PATH={wrong_child_db}")
    )

    # root-compose mount + worker service — mount is EXACT DIR (not a wrong child), plus a worker service.
    dir_compose = (
        "services:\n  core:\n    volumes:\n"
        "      - /var/lib/musubi/lifecycle:/var/lib/musubi/lifecycle\n"
        "  lifecycle-worker:\n    image: x\n"
    )
    file_compose = (
        "services:\n  core:\n    volumes:\n"
        "      - /var/lib/musubi/lifecycle-work.sqlite:/var/lib/musubi/lifecycle-work.sqlite\n"
    )
    child_compose = (
        "services:\n  core:\n    volumes:\n"
        "      - /var/lib/musubi/lifecycle/sub:/var/lib/musubi/lifecycle\n"
        "  lifecycle-worker:\n    image: x\n"
    )
    assert _resolves_canonical_mount(
        _compose_lifecycle_host_path(dir_compose)
    ) and _has_lifecycle_worker_service(dir_compose)
    assert not (
        _resolves_canonical_mount(_compose_lifecycle_host_path(file_compose))
        and _has_lifecycle_worker_service(file_compose)
    )
    assert not _resolves_canonical_mount(  # wrong child mount — REJECTED even with a worker service
        _compose_lifecycle_host_path(child_compose)
    )
    assert _compose_lifecycle_host_path(child_compose) == wrong_child_mount

    # backup.yml NAMED sqlite `.backup` command source — bound to the command, not the first path anywhere.
    dir_backup = (
        "- hosts: all\n  tasks:\n    - name: Back up sqlite lifecycle ledger\n"
        "      ansible.builtin.command:\n"
        '        cmd: sqlite3 /var/lib/musubi/lifecycle/work.sqlite ".backup /mnt/x.sqlite"\n'
    )
    file_backup = (
        "- hosts: all\n  tasks:\n    - name: Back up sqlite lifecycle ledger\n"
        "      ansible.builtin.command:\n"
        '        cmd: sqlite3 /var/lib/musubi/lifecycle-work.sqlite ".backup /mnt/x.sqlite"\n'
    )
    # WRONG: a comment mentions the DIR, but the actual command still reads the FILE -> still RED.
    comment_dir_command_file = (
        "- hosts: all\n  tasks:\n"
        "    # migrate to /var/lib/musubi/lifecycle/work.sqlite (the canonical DIR DB) later\n"
        "    - name: Back up sqlite lifecycle ledger\n"
        "      ansible.builtin.command:\n"
        '        cmd: sqlite3 /var/lib/musubi/lifecycle-work.sqlite ".backup /mnt/x.sqlite"\n'
    )
    assert _resolves_canonical_dir_db(_sqlite_backup_command_src(dir_backup))
    assert not _resolves_canonical_dir_db(_sqlite_backup_command_src(file_backup))
    assert _sqlite_backup_command_src(comment_dir_command_file) == _RETIRED_FILE_DB
    assert not _resolves_canonical_dir_db(_sqlite_backup_command_src(comment_dir_command_file))
    assert not _resolves_canonical_dir_db(  # wrong child in the command source — REJECTED
        _sqlite_backup_command_src(
            "- hosts: all\n  tasks:\n    - name: b\n      ansible.builtin.command:\n"
            f'        cmd: sqlite3 {wrong_child_db} ".backup /mnt/x.sqlite"\n'
        )
    )

    # runbook restore destination — DB-bearing, EXACT DB.
    assert _resolves_canonical_dir_db(
        _runbook_restore_dest(
            'cp -a "$SNAP/sqlite/work.sqlite" /var/lib/musubi/lifecycle/work.sqlite'
        )
    )
    assert not _resolves_canonical_dir_db(
        _runbook_restore_dest(
            'cp -a "$SNAP/sqlite/work.sqlite" /var/lib/musubi/lifecycle-work.sqlite'
        )
    )
    assert not _resolves_canonical_dir_db(  # wrong child restore target — REJECTED
        _runbook_restore_dest(f'cp -a "$SNAP/sqlite/work.sqlite" {wrong_child_db}')
    )

    # backup README — the OPERATIONAL storage statement is inspected, not the filename anywhere.
    assert _readme_resolves_dir(
        "## Stores\n- `lifecycle/work.sqlite` and cursor files copy hourly into /mnt/snapshots/sqlite/."
    )
    assert not _readme_resolves_dir(
        "## Stores\n- `lifecycle-work.sqlite` and cursor files copy hourly into /mnt/snapshots/sqlite/."
    )
    # MIXED: a historical/migration mention of the retired FILE, but the OPERATIONAL line names the DIR
    # -> PASSES (the history does not trip the red).
    assert _readme_resolves_dir(
        "## History\nBefore the DIR cutover the ledger lived at `lifecycle-work.sqlite`.\n\n"
        "## Stores\n- `lifecycle/work.sqlite` and cursor files copy hourly into /mnt/snapshots/sqlite/."
    )
    # Inverse MIXED: an operational FILE statement is RED even if the DIR name appears in history prose.
    assert not _readme_resolves_dir(
        "## History\nThe canonical DIR DB is `lifecycle/work.sqlite`.\n\n"
        "## Stores\n- `lifecycle-work.sqlite` and cursor files copy hourly into /mnt/snapshots/sqlite/."
    )
    assert _readme_operational_storage_line("no operational storage statement here") is None

    # ---- Yua exact-review near-misses (round 2): LITERAL equality, NAMED task, Stores section ----
    # (1) a TRAILING SLASH is NOT canonical — rstrip normalization would false-green an invalid filename
    assert not _resolves_canonical_dir_db("/var/lib/musubi/lifecycle/work.sqlite/")
    assert not _resolves_canonical_mount("/var/lib/musubi/lifecycle/")
    assert _resolves_canonical_dir_db("/var/lib/musubi/lifecycle/work.sqlite")  # exact still passes
    assert _resolves_canonical_mount("/var/lib/musubi/lifecycle")
    # (2) an UNRELATED canonical `.backup` FIRST must not mask the NAMED task reading the retired FILE
    unrelated_first = (
        "- hosts: all\n  tasks:\n"
        "    - name: Unrelated pre-backup\n      ansible.builtin.command:\n"
        '        cmd: sqlite3 /var/lib/musubi/lifecycle/work.sqlite ".backup /tmp/x.sqlite"\n'
        "    - name: Back up sqlite lifecycle ledger\n      ansible.builtin.command:\n"
        '        cmd: sqlite3 /var/lib/musubi/lifecycle-work.sqlite ".backup /mnt/x.sqlite"\n'
    )
    assert (
        _sqlite_backup_command_src(unrelated_first) == _RETIRED_FILE_DB
    )  # the NAMED task, not the first
    assert not _resolves_canonical_dir_db(_sqlite_backup_command_src(unrelated_first))
    # zero/duplicate named lifecycle-backup tasks fail CLOSED (None -> not canonical)
    assert (
        _sqlite_backup_command_src(
            "- hosts: all\n  tasks:\n    - name: other\n      command: echo x\n"
        )
        is None
    )
    assert (
        _sqlite_backup_command_src(
            unrelated_first + unrelated_first.split("- hosts: all\n  tasks:\n")[1]
        )
        is None
    )
    # (3) a History line naming the DIR *with hourly* BEFORE a Stores bullet naming the FILE is still RED
    assert not _readme_resolves_dir(
        "## History\n- previously `lifecycle/work.sqlite` copied hourly (pre-cutover)\n\n"
        "## Stores\n- `lifecycle-work.sqlite` and cursor files copy hourly into /mnt/snapshots/sqlite/."
    )
    # zero / duplicate Stores lifecycle bullets fail CLOSED (None)
    assert _readme_operational_storage_line("## Stores\n- artifact blobs rsync hourly") is None
    assert (
        _readme_operational_storage_line(
            "## Stores\n- `lifecycle/work.sqlite` copy hourly\n- `lifecycle-work.sqlite` copy hourly"
        )
        is None
    )
    # Yua exact-review (round 3): the heading must be EXACTLY '## Stores' — a prefix/suffix lookalike is NOT
    # the Stores section, so its DIR bullet must NOT green the README (the real Stores bullet still names FILE).
    assert not _readme_resolves_dir("## Stores history\n- `lifecycle/work.sqlite` copy hourly")
    assert not _readme_resolves_dir("## Stores-old\n- `lifecycle/work.sqlite` copy hourly")
    # exact '## Stores' (case-insensitive) still binds
    assert _readme_resolves_dir("## stores\n- `lifecycle/work.sqlite` copy hourly")
    assert (
        _readme_operational_storage_line("## Stores history\n- `lifecycle/work.sqlite` copy hourly")
        is None
    )


# ---- TASK 2: preserve-green anchor CONTROLS (UNMARKED — pass today, fail loudly on a FILE regress) -- #


def _yaml_tasks(playbook_text: str) -> list[dict[str, Any]]:
    """Every task mapping across every play in an Ansible playbook document."""
    doc = yaml.safe_load(playbook_text)
    tasks: list[dict[str, Any]] = []
    for play in doc if isinstance(doc, list) else []:
        for task in play.get("tasks", []) if isinstance(play, dict) else []:
            if isinstance(task, dict):
                tasks.append(task)
    return tasks


def _bash_scalar(text: str, name: str) -> str | None:
    m = re.search(rf'{name}="([^"]+)"', text)
    return m.group(1) if m else None


def test_p0c_anchor_ansible_compose_dir_mount_and_worker() -> None:
    """ANCHOR CONTROL: the production ansible compose template already binds the DIRECTORY
    /var/lib/musubi/lifecycle for BOTH core (:16) and worker (:52) and declares a lifecycle-worker service.
    A regress of either mount to the bare FILE, or dropping the worker, fails this loudly."""
    text = (_P0C_REPO_ROOT / "deploy/ansible/templates/docker-compose.yml.j2").read_text()
    mounts = _all_lifecycle_host_mounts(text)
    assert len(mounts) >= 2, f"expected core+worker lifecycle mounts, got {mounts!r}"
    assert all(_resolves_canonical_mount(m) for m in mounts), (
        f"ansible compose lifecycle mounts regressed off the exact DIR mount: {mounts!r}"
    )
    assert _has_lifecycle_worker_service(text), (
        "ansible compose template dropped the lifecycle-worker service"
    )


def test_p0c_anchor_ansible_env_production_dir() -> None:
    """ANCHOR CONTROL: env.production.j2:13 already sets LIFECYCLE_SQLITE_PATH to the canonical DIR DB."""
    path = _env_lifecycle_path(
        (_P0C_REPO_ROOT / "deploy/ansible/templates/env.production.j2").read_text()
    )
    assert _resolves_canonical_dir_db(path), (
        f"env.production.j2 LIFECYCLE_SQLITE_PATH regressed off the exact DIR DB: {path!r}"
    )
    assert path == _CANONICAL_DIR_DB


def test_p0c_anchor_bootstrap_creates_lifecycle_dir_with_musubi_0750() -> None:
    """ANCHOR CONTROL: bootstrap.yml:130 'Create Musubi data directories' owns musubi_data_dirs — which
    includes /var/lib/musubi/lifecycle — as musubi:musubi mode 0750. The canonical unit is a DIRECTORY, so
    this perms task IS the lock: a regress that drops the DIR from musubi_data_dirs, or loosens owner/mode,
    fails loudly."""
    group_vars = yaml.safe_load((_P0C_REPO_ROOT / "deploy/ansible/group_vars/all.yml").read_text())
    assert group_vars["musubi_service_user"] == "musubi"
    assert group_vars["musubi_service_group"] == "musubi"
    data_dirs = group_vars["musubi_data_dirs"]
    assert "/var/lib/musubi/lifecycle" in data_dirs, (
        f"lifecycle DIR dropped from musubi_data_dirs: {data_dirs!r}"
    )

    tasks = _yaml_tasks((_P0C_REPO_ROOT / "deploy/ansible/bootstrap.yml").read_text())
    mkdir = next((t for t in tasks if t.get("name") == "Create Musubi data directories"), None)
    assert mkdir is not None, "bootstrap.yml lost the 'Create Musubi data directories' task"
    spec = mkdir["ansible.builtin.file"]
    assert spec["state"] == "directory"
    assert spec["owner"] == "{{ musubi_service_user }}"
    assert spec["group"] == "{{ musubi_service_group }}"
    assert str(spec["mode"]) == "0750"
    assert mkdir["loop"] == "{{ musubi_data_dirs }}"


def test_p0c_anchor_live_scheduler_backup_dir() -> None:
    """ANCHOR CONTROL: the LIVE backup scheduler musubi-backup.sh:211 sources the canonical DIR DB."""
    src = _bash_scalar(
        (_P0C_REPO_ROOT / "deploy/backup/musubi-backup.sh").read_text(), "SQLITE_SRC"
    )
    assert _resolves_canonical_dir_db(src), (
        f"musubi-backup.sh SQLITE_SRC regressed off the exact DIR DB: {src!r}"
    )
    assert src == _CANONICAL_DIR_DB


def test_p0c_anchor_restore_yml_dir() -> None:
    """ANCHOR CONTROL: restore.yml (paired with the live scheduler) restores into the canonical DIR DB."""
    path = _first_lifecycle_sqlite((_P0C_REPO_ROOT / "deploy/backup/restore.yml").read_text())
    assert _resolves_canonical_dir_db(path), (
        f"restore.yml lifecycle restore target regressed off the exact DIR DB: {path!r}"
    )
    assert path == _CANONICAL_DIR_DB


# ---- TASK 3: migration CONTRACT spec (reference-candidate red-proof) — CONTRACT ONLY, R20-gated ----- #
# The FILE->DIR storage migration is DOWNSTREAM and R20-gated. Nothing here executes it; these tests
# encode the fail-closed CONTRACT the downstream task must satisfy, exercised against real tmp_path SQLite.


class _MigrationRefused(Exception):
    """A migration candidate refused + signalled (fail-closed), rather than silently picking a DB."""


class _VerifyFailed(Exception):
    """A migration candidate's pre-cutover verify rejected the DIR DB (bad integrity/schema/row count)."""


@dataclass
class _MigCandidate:
    #: fail-closed detector: True => REFUSE the migration (ambiguous source), False => proceed.
    detect_ambiguity: "Callable[[Path, Path], bool]"
    #: pre-cutover verify: raise _VerifyFailed on a bad DIR DB, else return.
    verify: "Callable[[Path, int], None]"
    #: rollback(old_file, dir_db, resumed_writes): honor the post-write rule.
    rollback: "Callable[[Path, Path, bool], None]"


def _mig_make_db(path: Path, rows: int, *, corrupt: bool = False, schema: bool = True) -> None:
    """Build a real WAL-checkpointed SQLite DB at ``path`` with ``rows`` lifecycle_events (optionally with
    no schema, or byte-corrupted so PRAGMA integrity_check fails)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        if schema:
            con.execute(
                "CREATE TABLE lifecycle_events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            con.executemany(
                "INSERT INTO lifecycle_events(payload) VALUES(?)", [(f"e{i}",) for i in range(rows)]
            )
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    if corrupt:
        data = bytearray(path.read_bytes())
        for i in range(100, min(len(data), 100 + 512)):
            data[i] = (data[i] + 0x5A) & 0xFF
        path.write_bytes(bytes(data))


def _db_present(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _mig_row_count(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0])
    finally:
        con.close()


def _ref_detect_ambiguity(old_file: Path, dir_db: Path) -> bool:
    """CONTRACT: fail closed if BOTH the retired FILE and the canonical DIR DB hold a database, or a
    sibling marker (locks/, vault-writelog.db) exists at BOTH parents — never silently pick one."""
    both_db = _db_present(old_file) and _db_present(dir_db)
    both_locks = (old_file.parent / "locks").exists() and (dir_db.parent / "locks").exists()
    both_writelog = (old_file.parent / "vault-writelog.db").exists() and (
        dir_db.parent / "vault-writelog.db"
    ).exists()
    return both_db or both_locks or both_writelog


def _ref_verify(dir_db: Path, expected_rows: int) -> None:
    """CONTRACT: before cutover run PRAGMA integrity_check + a schema check + a row-count check on the DIR
    DB; a bad DB must raise _VerifyFailed (never reach the started services)."""
    try:
        con = sqlite3.connect(dir_db)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise _VerifyFailed(f"integrity_check={row!r}")
            tables = {
                n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "lifecycle_events" not in tables:
                raise _VerifyFailed("missing lifecycle_events table")
            n = int(con.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0])
            if n != expected_rows:
                raise _VerifyFailed(f"row count {n} != expected {expected_rows}")
        finally:
            con.close()
    except sqlite3.DatabaseError as e:  # a malformed image surfaces here, not as integrity 'ok'
        raise _VerifyFailed(str(e)) from e


def _ref_rollback(old_file: Path, dir_db: Path, resumed_writes: bool) -> None:
    """CONTRACT (locked disposition): the old FILE is a PRE-migration snapshot. Restoring it onto the DIR is
    valid ONLY before resumed writes. AFTER the DIR DB has taken new writes:
    - the canonical DIR DB is PRESERVED with its current rows — the stale old FILE NEVER becomes canonical;
    - a compatibility export to the retired FILE (if kept for compat) MUST wal_checkpoint(TRUNCATE) then
      copy the CURRENT DIR DB to that EXACT retired-FILE target, so the FILE reflects the live DIR data —
      NOT the stale pre-migration snapshot, and NOT merely a side ``*.rollback-backup`` file."""
    if not resumed_writes:
        dir_db.write_bytes(old_file.read_bytes())  # valid pre-write restore of the snapshot
        return
    con = sqlite3.connect(dir_db)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    # DIR DB left intact + canonical; export the CURRENT DIR data to the EXACT retired FILE target for compat.
    old_file.write_bytes(dir_db.read_bytes())


def _wrong_silently_picks_one(old_file: Path, dir_db: Path) -> bool:
    return False  # ambiguous source, yet proceeds — silently picks one


def _wrong_skips_integrity(dir_db: Path, expected_rows: int) -> None:
    return None  # cutover without any integrity/schema/row check


def _wrong_restores_stale_file(old_file: Path, dir_db: Path, resumed_writes: bool) -> None:
    dir_db.write_bytes(
        old_file.read_bytes()
    )  # restores the stale FILE even post-write => data loss (DIR not preserved)


def _wrong_noop_rollback(old_file: Path, dir_db: Path, resumed_writes: bool) -> None:
    return (
        None  # does NOTHING — no compatibility export; the retired FILE stays at its stale snapshot
    )


def _wrong_side_backup_only(old_file: Path, dir_db: Path, resumed_writes: bool) -> None:
    # checkpoints + writes a SIDE `work.sqlite.rollback-backup`, but never exports the CURRENT DIR data to
    # the EXACT retired FILE target -> the retired FILE stays stale, so a compat consumer reads old data.
    con = sqlite3.connect(dir_db)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    (dir_db.parent / "work.sqlite.rollback-backup").write_bytes(dir_db.read_bytes())


def _migration_candidate(name: str) -> _MigCandidate:
    cand = _MigCandidate(_ref_detect_ambiguity, _ref_verify, _ref_rollback)
    if name == "silently_picks_one":
        cand.detect_ambiguity = _wrong_silently_picks_one
    elif name == "skips_integrity":
        cand.verify = _wrong_skips_integrity
    elif name == "restores_stale_file_post_write":
        cand.rollback = _wrong_restores_stale_file
    elif name == "noop_rollback":
        cand.rollback = _wrong_noop_rollback
    elif name == "side_backup_only_rollback":
        cand.rollback = _wrong_side_backup_only
    elif name != "correct":  # pragma: no cover - guard against a typo'd candidate name
        raise AssertionError(f"unknown migration candidate {name!r}")
    return cand


def _clause_ambiguity(cand: _MigCandidate, base: Path) -> None:
    """BOTH the FILE and the DIR DB hold a database -> the candidate MUST refuse (fail closed)."""
    old_file = base / "lifecycle-work.sqlite"
    dir_db = base / "lifecycle" / "work.sqlite"
    _mig_make_db(old_file, 3)
    _mig_make_db(dir_db, 3)
    if not cand.detect_ambiguity(old_file, dir_db):
        raise DefectStillPresent(
            "ambiguity: migration did NOT fail closed when BOTH the retired FILE and the canonical DIR DB "
            "hold a database — it would silently pick one instead of refusing + signalling."
        )


def _clause_verify(cand: _MigCandidate, base: Path) -> None:
    """A byte-corrupted DIR DB -> the candidate MUST verify (integrity/schema/row) and reject it pre-cutover."""
    dir_db = base / "lifecycle" / "work.sqlite"
    _mig_make_db(dir_db, 5, corrupt=True)
    try:
        cand.verify(dir_db, 5)
    except _VerifyFailed:
        return  # correctly caught the bad DB before cutover
    raise DefectStillPresent(
        "verify: cutover proceeded WITHOUT an integrity/schema/row check — a corrupt DIR DB would reach "
        "the started services."
    )


def _clause_rollback(cand: _MigCandidate, base: Path) -> None:
    """LOCKED post-write rollback disposition. The DIR DB has taken new writes (N=7 rows) past the
    pre-migration FILE snapshot (3 rows). A conforming rollback MUST:
    (a) PRESERVE the canonical DIR DB with its current N=7 rows — the stale old FILE never becomes canonical
        (so a rollback that restores the 3-row snapshot onto the DIR fails HERE); AND
    (b) leave the retired FILE — if kept for compatibility — holding the CURRENT DIR data (N=7), produced by
        wal_checkpoint(TRUNCATE)+copy of the DIR to that EXACT target; a no-op (FILE stays at 3) or a
        side-``*.rollback-backup``-only disposition (FILE never updated) fails HERE.
    This is not usable proof unless BOTH targets are asserted at N=7 — a rowcount-of-DIR-only check let a
    no-op pass."""
    n = 7
    old_file = base / "lifecycle-work.sqlite"
    dir_db = base / "lifecycle" / "work.sqlite"
    _mig_make_db(old_file, 3)  # pre-migration snapshot (stale)
    _mig_make_db(dir_db, n)  # DIR DB has resumed writes post-cutover
    cand.rollback(old_file, dir_db, True)

    # (a) the canonical DIR DB is preserved with its current rows, and IS the canonical target (the DIR db).
    assert dir_db.name == "work.sqlite" and dir_db.parent.name == "lifecycle"
    live = _mig_row_count(dir_db)
    if live != n:
        raise DefectStillPresent(
            f"rollback: the canonical DIR DB was NOT preserved — it holds {live} rows (expected the current "
            f"{n}); a post-write rollback must never let the stale old FILE become canonical."
        )
    # (b) the retired FILE, kept for compat, reflects the CURRENT DIR data (N=7) at the EXACT target — not
    #     the stale 3-row snapshot, and not merely a side *.rollback-backup file.
    if not _db_present(old_file) or _mig_row_count(old_file) != n:
        exported = _mig_row_count(old_file) if _db_present(old_file) else None
        raise DefectStillPresent(
            f"rollback: the compatibility export to the retired FILE is not usable — {old_file.name} holds "
            f"{exported} rows (expected the CURRENT DIR's {n}). A no-op or side-backup-only disposition "
            "leaves the retired FILE stale; the export must wal_checkpoint(TRUNCATE)+copy the CURRENT DIR "
            "DB to that exact target so BOTH hold N=7."
        )


_MIGRATION_CLAUSES: dict[str, "Callable[[_MigCandidate, Path], None]"] = {
    "ambiguity": _clause_ambiguity,
    "verify": _clause_verify,
    "rollback": _clause_rollback,
}

#: each plausible-wrong candidate violates exactly one contract clause.
_MIGRATION_WRONG_CLAUSE: dict[str, str] = {
    "silently_picks_one": "ambiguity",
    "skips_integrity": "verify",
    "restores_stale_file_post_write": "rollback",
    "noop_rollback": "rollback",
    "side_backup_only_rollback": "rollback",
}


@pytest.mark.parametrize("candidate", ["correct", *sorted(_MIGRATION_WRONG_CLAUSE)])
def test_p0c_storage_migration_contract_red_proof(candidate: str, tmp_path: Path) -> None:
    """RERUNNABLE reference-candidate red-proof of the FILE->DIR storage-migration CONTRACT (exercised on
    real tmp_path SQLite). The CORRECT reference satisfies all three clauses; each plausible-wrong candidate
    fails at its INTENDED clause and passes the others. CONTRACT only — no migration is executed."""
    cand = _migration_candidate(candidate)
    if candidate == "correct":
        for clause, check in _MIGRATION_CLAUSES.items():
            check(cand, tmp_path / f"correct-{clause}")
        return
    wrong_clause = _MIGRATION_WRONG_CLAUSE[candidate]
    with pytest.raises(DefectStillPresent) as ei:
        _MIGRATION_CLAUSES[wrong_clause](cand, tmp_path / f"{candidate}-{wrong_clause}")
    assert str(ei.value).startswith(f"{wrong_clause}:"), (
        f"{candidate} failed at the wrong clause: {ei.value}"
    )
    for other, check in _MIGRATION_CLAUSES.items():
        if other != wrong_clause:
            check(
                cand, tmp_path / f"{candidate}-{other}"
            )  # the candidate's OTHER clauses must pass


def test_p0c_storage_migration_verify_checks_all_three(tmp_path: Path) -> None:
    """GREEN mechanism proof: the reference verify independently rejects (a) a byte-corrupt DB via
    integrity_check, (b) a schema-less DB, (c) a row-count mismatch, and ACCEPTS a good DB — proving all
    three pre-cutover checks are real, and that a valid pre-write snapshot restore is honored."""
    good = tmp_path / "good" / "work.sqlite"
    _mig_make_db(good, 4)
    _ref_verify(good, 4)  # accepts

    corrupt = tmp_path / "corrupt" / "work.sqlite"
    _mig_make_db(corrupt, 4, corrupt=True)
    with pytest.raises(_VerifyFailed):
        _ref_verify(corrupt, 4)

    noschema = tmp_path / "noschema" / "work.sqlite"
    _mig_make_db(noschema, 0, schema=False)
    with pytest.raises(_VerifyFailed):
        _ref_verify(noschema, 0)

    with pytest.raises(_VerifyFailed):
        _ref_verify(good, 999)  # row-count mismatch

    # a PRE-write rollback validly restores the snapshot (the other half of the rollback rule)
    old_file = tmp_path / "prewrite" / "lifecycle-work.sqlite"
    dir_db = tmp_path / "prewrite" / "lifecycle" / "work.sqlite"
    _mig_make_db(old_file, 2)
    _mig_make_db(dir_db, 9)
    _ref_rollback(old_file, dir_db, False)  # pre-write: snapshot restore is valid
    assert _mig_row_count(dir_db) == 2


#: The ONE authored migration format supported in this tests-only slice (Yua round-3 ruling): a shell
#: script parsed by REAL command invocations, NOT whole-text token co-occurrence. A .py/.yml migration
#: would need AST/yaml-task semantics; it is out of scope here and is NOT accepted by token matching.
_MIGRATION_ARTIFACT_SUFFIX = ".sh"

#: Shell line heads that are NOT a data command — their operands/args are never migration evidence.
_SH_NONCOMMAND_HEADS = frozenset(
    {
        "echo",
        "printf",
        "cat",
        "true",
        "false",
        ":",
        "set",
        "export",
        "local",
        "declare",
        "read",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "function",
        "return",
        "exit",
        "shift",
        "trap",
        "[",
        "[[",
        "test",
        "source",
        ".",
    }
)


def _resolve_sh_operand(tok: str, assigns: Mapping[str, str]) -> str:
    """Resolve a `$VAR` / `${VAR}` operand against literal assignments; a non-variable token is literal."""
    m = re.fullmatch(r"\$\{?(\w+)\}?", tok)
    return assigns.get(m.group(1), tok) if m is not None else tok


#: shell composition operators — a migration command joined by these is unsupported (fail closed).
_SH_COMPOSITION = frozenset({"&&", "||", "|", ";", "&"})
#: exact SQL structures (whole-argument) for the two required pre-cutover checks (case-insensitive).
_RE_PRAGMA_INTEGRITY = re.compile(r"(?i)^\s*PRAGMA\s+integrity_check\s*;?\s*$")
_RE_PRAGMA_CHECKPOINT = re.compile(
    r"(?i)^\s*PRAGMA\s+wal_checkpoint(\s*\(\s*TRUNCATE\s*\))?\s*;?\s*$"
)


def _sh_migration_ok(text: str) -> bool:
    """ORDERED command-event analysis of a .sh migration (Yua round-4 ruling). True ONLY when the parsed
    real-command sequence is EXACTLY: one `sqlite3 <OLD> "PRAGMA integrity_check"` AND one
    `sqlite3 <OLD> "PRAGMA wal_checkpoint[(TRUNCATE)]"` — the DB operand resolving EXACTLY to the retired
    FILE and the SQL being an ACTUAL PRAGMA (not a SELECT/echo string) — BOTH occurring BEFORE exactly one
    `cp|mv <OLD> <NEW>` move with EXACTLY TWO non-option operands resolving retired-FILE -> canonical-DIR-DB.
    Duplicate/ambiguous move or check commands, a check on the wrong DB, a marker-string (SELECT/echo)
    instead of a real PRAGMA, a multi-operand cp, checks AFTER the move, or shell composition
    (&&/||/|/;/$()/backtick) around a migration command all fail CLOSED."""
    assigns: dict[str, str] = {}
    events: list[
        tuple[str, bool]
    ] = []  # ordered: ('integrity'|'checkpoint'|'move', valid_on_our_units)
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#"):
            continue
        hd = re.search(
            r"<<-?\s*[\"']?(\w+)[\"']?", line
        )  # here-doc body is data — skip it entirely
        if hd is not None:
            term = hd.group(1)
            while i < len(lines) and lines[i].strip() != term:
                i += 1
            i += 1
            continue
        am = re.match(r"^(\w+)=(\S+)$", line)  # a bare `VAR=value` assignment (no command)
        if am is not None:
            assigns[am.group(1)] = am.group(2).strip("\"'")
            continue
        try:
            toks = shlex.split(line, comments=True)
        except ValueError:
            return False  # unparseable shell -> fail closed
        if not toks:
            continue
        base = toks[0].rsplit("/", 1)[-1]
        if base in _SH_NONCOMMAND_HEADS:
            continue
        # a migration command must be a SIMPLE command — reject shell composition / substitution
        if base in {"cp", "mv", "sqlite3"} and (
            any(t in _SH_COMPOSITION for t in toks) or any("$(" in t or "`" in t for t in toks)
        ):
            return False
        if base in {"cp", "mv"}:
            ops = [_resolve_sh_operand(t, assigns) for t in toks[1:] if not t.startswith("-")]
            touches = _RETIRED_FILE_DB in ops or _CANONICAL_DIR_DB in ops
            if touches:  # a move candidate on our units — valid ONLY as exactly [OLD, NEW]
                events.append(("move", ops == [_RETIRED_FILE_DB, _CANONICAL_DIR_DB]))
        elif base == "sqlite3":
            operands = [t for t in toks[1:] if not t.startswith("-")]
            if len(operands) < 2:
                continue
            db = _resolve_sh_operand(operands[0], assigns)
            sql = operands[1]
            on_old = db == _RETIRED_FILE_DB
            if _RE_PRAGMA_INTEGRITY.match(sql):
                events.append(("integrity", on_old))
            elif _RE_PRAGMA_CHECKPOINT.match(sql):
                events.append(("checkpoint", on_old))
    moves = [k for k, _ in enumerate(events) if events[k][0] == "move"]
    if len(moves) != 1 or not events[moves[0]][1]:  # exactly one, exactly OLD->NEW
        return False
    before = events[: moves[0]]
    integ = [
        e for e in before if e[0] == "integrity" and e[1]
    ]  # real PRAGMA on OLD, before the move
    chkpt = [e for e in before if e[0] == "checkpoint" and e[1]]
    # any check present at all (even on the wrong DB / after the move) that is not the single valid pre-move
    # one is ambiguity -> fail closed
    total_integ = [e for e in events if e[0] == "integrity"]
    total_chkpt = [e for e in events if e[0] == "checkpoint"]
    return len(integ) == 1 and len(chkpt) == 1 and len(total_integ) == 1 and len(total_chkpt) == 1


def _is_lifecycle_migration_artifact(suffix: str, text: str) -> bool:
    """True iff a deploy artifact actually BUILDS the FILE->DIR lifecycle storage migration. NARROWED to the
    single supported ``.sh`` format (Yua round-3 ruling) and recognized by PARSED REAL COMMANDS, not token
    co-occurrence: it must contain a real ``cp``/``mv`` whose resolved operands move the retired FILE ->
    the canonical DIR DB, AND a real ``sqlite3`` command running ``integrity_check``, AND one running
    ``wal_checkpoint``. Tokens inside echo/printf/here-doc/comments/assignments/docstrings, or in a
    ``.py``/``.yml`` file, do NOT count."""
    if suffix != _MIGRATION_ARTIFACT_SUFFIX:
        return False
    return _sh_migration_ok(text)


def _lifecycle_storage_migration_task_files() -> list[str]:
    """deploy/ files that BUILD a FILE->DIR lifecycle storage migration per ``_is_lifecycle_migration_artifact``.
    Today none exist (deploy/migration/ is the POC->v1 Qdrant migration, unrelated to lifecycle SQLite
    storage; the runbooks are prose)."""
    deploy = _P0C_REPO_ROOT / "deploy"
    hits: list[str] = []
    for p in sorted(deploy.rglob("*")):
        if (
            not p.is_file() or p.suffix != _MIGRATION_ARTIFACT_SUFFIX
        ):  # only the supported .sh format
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):  # pragma: no cover - defensive
            continue
        if _is_lifecycle_migration_artifact(p.suffix, text):
            hits.append(str(p.relative_to(_P0C_REPO_ROOT)))
    return hits


# ---- fixtures for the migration-task DETECTION discriminator (operation-shaped, executable-only) ----- #
_MIG_BOTH_PATHS = (
    "src /var/lib/musubi/lifecycle-work.sqlite dst /var/lib/musubi/lifecycle/work.sqlite\n"
)
#: prose-only: both paths + operation + contract markers, but a .md is NOT an executable task.
_MIG_PROSE_MD = (
    "# Migration runbook\n"
    "Copy /var/lib/musubi/lifecycle-work.sqlite to /var/lib/musubi/lifecycle/work.sqlite.\n"
    "Run PRAGMA integrity_check then wal_checkpoint. Use cp -a for the move.\n"
)
#: unrelated executable: both paths co-occur, but no migration operation + no contract markers.
_MIG_UNRELATED_SH = (
    "#!/usr/bin/env bash\n"
    "# audit that both /var/lib/musubi/lifecycle-work.sqlite and "
    "/var/lib/musubi/lifecycle/work.sqlite exist\n"
    "ls -l /var/lib/musubi/lifecycle-work.sqlite /var/lib/musubi/lifecycle/work.sqlite\n"
)
#: a real contract-shaped migration task: both paths + a copy operation + verify + checkpoint markers.
_MIG_REAL_TASK_SH = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "OLD=/var/lib/musubi/lifecycle-work.sqlite\n"
    "NEW=/var/lib/musubi/lifecycle/work.sqlite\n"
    'if [[ -s "$OLD" && -s "$NEW" ]]; then echo refuse-ambiguous >&2; exit 3; fi\n'
    'sqlite3 "$OLD" "PRAGMA integrity_check"\n'
    'sqlite3 "$OLD" "PRAGMA wal_checkpoint(TRUNCATE)"\n'
    'cp -a "$OLD" "$NEW"\n'
)


def test_p0c_storage_migration_task_detection_discriminates() -> None:
    """GREEN mechanism proof for correction (5): the migration-task detector is OPERATION-SHAPED and
    EXECUTABLE-ONLY. A prose .md that names both paths (even with operation/marker words) does NOT count;
    an unrelated .sh that merely co-mentions both paths without the operation + contract markers does NOT
    count; only a real contract-shaped executable task (both paths + a data-move operation + verify +
    checkpoint markers) counts — which is what would flip the UNBUILT red green."""
    # executable-only: prose .md never counts, even shaped like a migration
    assert not _is_lifecycle_migration_artifact(".md", _MIG_PROSE_MD)
    # mere co-occurrence in an executable is NOT a migration
    assert not _is_lifecycle_migration_artifact(".sh", _MIG_UNRELATED_SH)
    # both paths alone, without operation/markers, is NOT a migration
    assert not _is_lifecycle_migration_artifact(".py", _MIG_BOTH_PATHS)
    # a real contract-shaped executable task DOES count (flips the red)
    assert _is_lifecycle_migration_artifact(".sh", _MIG_REAL_TASK_SH)
    # …and the SAME real content as .md prose still does NOT count (suffix gate)
    assert not _is_lifecycle_migration_artifact(".md", _MIG_REAL_TASK_SH)
    # each contract marker is load-bearing: dropping operation, verify, or checkpoint drops the hit
    assert not _is_lifecycle_migration_artifact(
        ".sh", _MIG_REAL_TASK_SH.replace("cp -a", "true #").replace(".backup", "x")
    )
    assert not _is_lifecycle_migration_artifact(
        ".sh", _MIG_REAL_TASK_SH.replace("integrity_check", "noop")
    )
    assert not _is_lifecycle_migration_artifact(
        ".sh", _MIG_REAL_TASK_SH.replace("wal_checkpoint", "noop")
    )
    # Yua exact-review (round 2): EVIDENCE only inside `#` comments does NOT count — a script whose ONLY
    # non-comment statement is `true` is not a migration even if every path/operation/marker is in comments.
    comment_only_sh = (
        "#!/bin/sh\n"
        "# migrate /var/lib/musubi/lifecycle-work.sqlite -> /var/lib/musubi/lifecycle/work.sqlite\n"
        "# cp -a old new; sqlite3 integrity_check; wal_checkpoint(TRUNCATE)\n"
        "true\n"
    )
    assert not _is_lifecycle_migration_artifact(".sh", comment_only_sh)
    # each evidence class independently: moving ONE marker out of a comment is still insufficient alone
    for marker in (
        "/var/lib/musubi/lifecycle/work.sqlite",
        "cp -a",
        "integrity_check",
        "wal_checkpoint",
    ):
        one_uncommented = (
            comment_only_sh.replace(f"# {marker}", marker, 1)
            if f"# {marker}" in comment_only_sh
            else comment_only_sh + marker + "\n"
        )
        assert not _is_lifecycle_migration_artifact(".sh", one_uncommented)

    # ---- Yua exact-review (round 3): PARSED real commands, NOT whole-text token co-occurrence ----
    # markers inside an echo/printf STRING argument are not real commands (the line head is echo/printf)
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        '#!/bin/sh\necho "cp -a /var/lib/musubi/lifecycle-work.sqlite '
        '/var/lib/musubi/lifecycle/work.sqlite integrity_check wal_checkpoint"\ntrue\n',
    )
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        '#!/bin/sh\nprintf "%s" "cp -a /var/lib/musubi/lifecycle-work.sqlite '
        '/var/lib/musubi/lifecycle/work.sqlite integrity_check wal_checkpoint"\n',
    )
    # a here-doc BODY is data, not commands — cp/sqlite3 lines inside it do not count
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        "#!/bin/sh\ncat <<EOF\ncp -a /var/lib/musubi/lifecycle-work.sqlite "
        "/var/lib/musubi/lifecycle/work.sqlite\nsqlite3 x integrity_check\n"
        "sqlite3 x wal_checkpoint\nEOF\ntrue\n",
    )
    # tokens only in ASSIGNMENTS (a MSG=... string), with no real move command, do not count
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        "#!/bin/sh\nOLD=/var/lib/musubi/lifecycle-work.sqlite\n"
        "NEW=/var/lib/musubi/lifecycle/work.sqlite\n"
        'MSG="cp -a integrity_check wal_checkpoint"\ntrue\n',
    )
    # the move OPERANDS must resolve old -> new; a REVERSED move (new -> old) does NOT count
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        "#!/bin/sh\nOLD=/var/lib/musubi/lifecycle-work.sqlite\n"
        "NEW=/var/lib/musubi/lifecycle/work.sqlite\n"
        'cp -a "$NEW" "$OLD"\nsqlite3 "$OLD" integrity_check\nsqlite3 "$OLD" wal_checkpoint\n',
    )
    # format is NARROWED to .sh: a .py docstring or .yml metadata carrying every token is NOT a migration
    assert not _is_lifecycle_migration_artifact(
        ".py",
        '"""cp /var/lib/musubi/lifecycle-work.sqlite /var/lib/musubi/lifecycle/work.sqlite '
        'shutil.copy integrity_check wal_checkpoint"""\nprint("noop")\n',
    )
    assert not _is_lifecycle_migration_artifact(
        ".yml",
        'name: "cp -a /var/lib/musubi/lifecycle-work.sqlite '
        '/var/lib/musubi/lifecycle/work.sqlite integrity_check wal_checkpoint"\ntasks: []\n',
    )
    # POSITIVE control: the real contract-shaped .sh (variable-resolved operands) DOES count
    assert _is_lifecycle_migration_artifact(".sh", _MIG_REAL_TASK_SH)

    # ---- Yua exact-review (round 4): ORDERED command-event semantics, not per-marker presence ----
    _mig_head = (
        "#!/bin/sh\nOLD=/var/lib/musubi/lifecycle-work.sqlite\n"
        "NEW=/var/lib/musubi/lifecycle/work.sqlite\n"
    )
    # (1) move BEFORE the checks + the checks are SELECT marker STRINGS on the WRONG db -> not a migration
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'cp -a "$OLD" "$NEW"\n'
        "sqlite3 /tmp/unrelated.db \"SELECT 'integrity_check'\"\n"
        "sqlite3 /tmp/unrelated.db \"SELECT 'wal_checkpoint'\"\n",
    )
    # (2) REAL PRAGMAs but on the WRONG db (not OLD) -> the verify did not run on the migrated unit
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'sqlite3 /tmp/unrelated.db "PRAGMA integrity_check"\n'
        'sqlite3 /tmp/unrelated.db "PRAGMA wal_checkpoint(TRUNCATE)"\ncp -a "$OLD" "$NEW"\n',
    )
    # (3) real PRAGMAs on OLD, then a MULTI-OPERAND cp (3 operands) — not a clean OLD->NEW move
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'sqlite3 "$OLD" "PRAGMA integrity_check"\n'
        'sqlite3 "$OLD" "PRAGMA wal_checkpoint(TRUNCATE)"\ncp -a /tmp/extra "$OLD" "$NEW"\n',
    )
    # ordering + uniqueness fail-closed: checks-after-move, a duplicate move, and && composition all reject
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'cp -a "$OLD" "$NEW"\nsqlite3 "$OLD" "PRAGMA integrity_check"\n'
        'sqlite3 "$OLD" "PRAGMA wal_checkpoint"\n',
    )
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'sqlite3 "$OLD" "PRAGMA integrity_check"\n'
        'sqlite3 "$OLD" "PRAGMA wal_checkpoint"\ncp -a "$OLD" "$NEW"\ncp -a "$OLD" "$NEW"\n',
    )
    assert not _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'sqlite3 "$OLD" "PRAGMA integrity_check" && cp -a "$OLD" "$NEW"\n',
    )
    # POSITIVE variable control: real PRAGMAs on OLD (via $OLD) BEFORE a single 2-operand move -> counts
    assert _is_lifecycle_migration_artifact(
        ".sh",
        _mig_head + 'sqlite3 "$OLD" "PRAGMA integrity_check"\n'
        'sqlite3 "$OLD" "PRAGMA wal_checkpoint(TRUNCATE)"\ncp -a "$OLD" "$NEW"\n',
    )

    # today the REAL deploy tree builds no such task -> the UNBUILT red stays RED
    assert _lifecycle_storage_migration_task_files() == []


_P0C_MIGRATION_UNBUILT_REASON = (
    "no FILE->DIR lifecycle storage-migration task is BUILT in deploy/ yet — no artifact bridges the "
    "retired FILE /var/lib/musubi/lifecycle-work.sqlite onto the canonical DIR DB "
    "/var/lib/musubi/lifecycle/work.sqlite. The migration is DOWNSTREAM + R20-gated (maintenance-mode); "
    "this red marks it UNBUILT (not un-run). Flips green when the migration task is AUTHORED per the "
    "contract in c6b-phase1-source-cut-plan.md §E — NOT when it is executed."
)


@pytest.mark.xfail(raises=DefectStillPresent, strict=True, reason=_P0C_MIGRATION_UNBUILT_REASON)
def test_p0c_storage_migration_task_unbuilt() -> None:
    tasks = _lifecycle_storage_migration_task_files()
    if not tasks:
        raise DefectStillPresent(
            "no lifecycle FILE->DIR storage-migration task is built under deploy/ yet (deploy/migration/ is "
            "the unrelated POC->v1 Qdrant migration). The task is downstream + R20-gated; author it per the "
            "§E migration contract before source cutover."
        )
