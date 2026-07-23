"""LifecycleTransitionCoordinator — C6b Phase-1 durable-intent outbox (S2 admission + S3 apply).

``transition()`` is the full lifecycle transition:

1. **operation_key idempotency** (before the cap): an existing row for the key short-circuits
   to its recorded outcome with a stable event_id; the same key with a DIFFERENT intent digest
   is an ``operation_key_conflict``. A concurrent same-key insert race re-resolves on the PK.
2. **Durable admission** (S2): write a ``PENDING`` row inside ONE ``BEGIN IMMEDIATE`` that
   enforces a global non-terminal **cap** and **one active intent per ``(collection, object_id)``**
   (the partial unique index ``ux_active_intent``). A bounded ``Err`` here leaves Qdrant untouched.
3. **Persisted event before mutation** (S3): from an exact pre-apply read, build a canonical
   :class:`~musubi.types.lifecycle_event.LifecycleEvent` and persist its JSON on the outbox row
   BEFORE any Qdrant mutation, so a post-crash finalize needs no fabricated fields.
4. **Conditional apply** (S3): a server-side version-fenced ``set_payload`` + a full readback that
   proves identity/namespace/version/state/every-key + a SHA over the actual projection.
5. **Marker + APPLIED** (S3): the effective-apply marker and ``PENDING→APPLIED`` commit together.
6. **Atomic finalize** (S3): the 8-column FINAL ``lifecycle_events`` row and ``APPLIED→FINAL``
   move in ONE transaction (R8 forward guard), stamping ``terminal_epoch``.

Outcomes are three-way: ``Ok(TransitionFinal)`` (confirmed apply + finalize), ``Ok(TransitionPending)``
(corrupt readback, or a transient/unknown apply failure, or a post-commit finalize fault — all left
for the S4 reconciler), or a bounded ``Err``. A known version fence and a pre-mutation validation
failure are terminal (ABANDONED). Reconciliation/leases (S4) and the durable maintenance/rollback
barrier plus bounded terminal cleanup (S6) are implemented here.

Connection + schema come from the shared lifecycle store (WAL + busy_timeout). A private
``_checkpoint(name)`` seam (default no-op) lets tests inject a deterministic fault/crash at a named
boundary; it is not a public switch and performs no production ``os._exit``.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import ValidationError
from qdrant_client import models

from musubi.lifecycle import store
from musubi.observability.registry import Counter, Gauge, default_registry
from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.lifecycle_event import LifecycleEvent

log = logging.getLogger(__name__)

#: Default global cap on non-terminal (PENDING/APPLIED) outbox rows. A positive int;
#: there is no unbounded/None option (admission must always have a bound).
DEFAULT_PENDING_CAP = 10_000

#: Default lease TTL (seconds) — a reconciler claim stamps ``lease_expires_epoch = now + ttl``.
DEFAULT_LEASE_TTL = 30.0

#: Default reconciler retry backoff (seconds): ``min(base * 2**(attempts-1), max)``.
DEFAULT_BACKOFF_BASE = 1.0
DEFAULT_BACKOFF_MAX = 300.0

#: Deterministic default patch timestamps so an intent constructed without explicit
#: ``updated_at`` yields a reproducible minimal patch (production callers pass real
#: values). Matches the accepted contract's fixed patch epoch.
_FIXED_UPDATED_AT = "2026-07-13T00:00:00+00:00"
_FIXED_UPDATED_EPOCH = datetime.fromisoformat(_FIXED_UPDATED_AT).timestamp()

#: R19 bounded observability (S5) — registered on the process-wide default registry so the worker
#: ``/metrics`` scrape and the contract tests see the SAME instruments. The gauge is UNLABELED
#: (a cap-backstop depth signal); the failures counter's ONLY label is the bounded external
#: ``class`` in ``{terminal, transient}`` — a runtime ``unknown`` classification stays durably
#: ``unknown`` in the row but maps to the external ``transient`` class (never an ``unknown`` label).
#: No object/namespace/operation identifier ever enters a metric name or label.
_OUTBOX_PENDING: Gauge = default_registry().gauge(
    "musubi_lifecycle_outbox_pending",
    "Current non-terminal (PENDING/APPLIED) lifecycle outbox depth.",
)
_OUTBOX_MUTATION_FAILURES: Counter = default_registry().counter(
    "musubi_lifecycle_outbox_mutation_failures_total",
    "Lifecycle outbox mutation failures, by bounded external class {terminal, transient}.",
    ("class",),
)


@dataclass(frozen=True)
class TransitionIntent:
    """A requested lifecycle transition. ``collection``/``object_id``/``namespace``/
    ``expected_version`` are required at the canonical boundary (no ``None``);
    ``operation_key`` is optional (the coordinator derives a stable canonical key)."""

    collection: str
    object_id: str
    namespace: str
    expected_version: int
    target_state: str
    actor: str
    reason: str
    operation_key: str | None = None
    updated_at: str = _FIXED_UPDATED_AT
    updated_epoch: float = _FIXED_UPDATED_EPOCH
    superseded_by: str | None = None
    supersedes: tuple[str, ...] = ()
    merged_from: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    promoted_to: str | None = None
    promoted_at: str | None = None


@dataclass(frozen=True)
class TransitionPending:
    """Admitted, durably recorded, not yet applied. The public success outcome of S2."""

    operation_key: str
    event_id: str
    kind: str = "pending"


def is_transition_pending(value: object) -> TypeGuard[TransitionPending]:
    """Recognize the public Pending variant by its stable discriminator and identifiers."""
    return isinstance(value, TransitionPending) or (
        getattr(value, "kind", None) == "pending"
        and isinstance(getattr(value, "operation_key", None), str)
        and bool(getattr(value, "operation_key", ""))
        and isinstance(getattr(value, "event_id", None), str)
        and bool(getattr(value, "event_id", ""))
    )


@dataclass(frozen=True)
class TransitionFinal:
    """Applied + finalized. Produced by S3 (conditional apply + full-readback finalize)."""

    operation_key: str
    event_id: str
    kind: str = "final"


@dataclass(frozen=True)
class TransitionError:
    """A bounded, non-secret failure outcome. ``code`` is a stable machine token."""

    code: str


TransitionOutcome = TransitionFinal | TransitionPending


@dataclass(frozen=True)
class ReconcileReport:
    """The outcome of one ``reconcile_once`` pass — how many due rows were claimed and how each
    claimed row was disposed (a claimed row is counted in exactly one of the disposition fields, or
    stays counted only in ``claimed`` if it is still mid-flight after a crash within the pass)."""

    claimed: int = 0
    finalized: int = 0
    pending: int = 0
    abandoned: int = 0
    failed: int = 0


@dataclass(frozen=True)
class RollbackDone:
    """A completed rollback/abort under the durable maintenance generation."""

    generation: int
    kind: str = "rolled_back"


@dataclass(frozen=True)
class CleanupReport:
    """One bounded terminal-row cleanup result."""

    deleted: int = 0
    remaining_eligible: int = 0
    terminal_total: int = 0


@dataclass(frozen=True)
class CustomIntentContext:
    """The claimed-intent context handed to a registered non-transition intent handler (C4/ART-001).

    ``owner_token`` is the coordinator's fresh, never-reused per-claim lease token — the handler
    uses it as the never-reused publish owner (the ABA fence). The handler MUST mint its own
    never-reused ``generation`` per attempt, stage work under (generation, owner_token), publish by
    a conditional head replace, and return ``'confirmed'`` (published + won), ``'fence'`` (a stale/
    lost attempt — terminal), or ``'retry'`` (transient — keep PENDING + backoff).

    ``patch_json`` is the durable intent payload persisted at admission (DATA-001 Phase 2): the
    handler recomputes its effect from this alone, so a crash after admission replays from disk with
    no caller memory. ``None`` for intents admitted without a payload (e.g. the artifact_index wrapper,
    which re-derives from Qdrant + the on-disk blob)."""

    operation_key: str
    object_id: str
    collection: str
    namespace: str
    owner_token: str
    patch_json: str | None = None


class CleanupConfigError(ValueError):
    """Raised when terminal cleanup receives an unsafe cutoff or batch bound."""


def _validate_pending_cap(cap: object) -> int:
    """The cap must be a positive int. ``bool`` is a subclass of ``int`` — reject it
    explicitly; reject any non-int type and any value ``<= 0``. No None/unbounded."""
    if isinstance(cap, bool) or not isinstance(cap, int):
        raise TypeError(f"pending_cap must be an int (not bool), got {type(cap).__name__}")
    if cap <= 0:
        raise ValueError(f"pending_cap must be a positive int, got {cap}")
    return cap


def _validate_lease_ttl(ttl: object) -> float:
    """The lease TTL must be a positive, finite number (not bool)."""
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise TypeError(f"lease_ttl must be a number (not bool), got {type(ttl).__name__}")
    ttl = float(ttl)
    if not (ttl > 0 and ttl < float("inf")):
        raise ValueError(f"lease_ttl must be a positive finite float, got {ttl}")
    return ttl


def _validate_positive_float(name: str, value: object) -> float:
    """A positive, finite float (not bool) — for the reconciler backoff base/max."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number (not bool), got {type(value).__name__}")
    v = float(value)
    if not (v > 0 and v < float("inf")):
        raise ValueError(f"{name} must be a positive finite float, got {v}")
    return v


class _CapExceeded(Exception):
    """Internal sentinel: the atomic admission found the non-terminal backlog at/over
    the cap. ``transition()`` maps it to ``Err(code='cap_exceeded')``; no row is
    written and Qdrant is untouched."""


class _AlreadyExists(Exception):
    """Internal sentinel: the SERIALIZED admission (inside ``BEGIN IMMEDIATE``, before the cap
    gate) found a row already exists for this operation_key — a caller whose out-of-transaction
    ``_replay`` missed a concurrent winner (TOCTOU). ``transition()`` resolves it with the SAME
    idempotency semantics as ``_replay`` (BEFORE the cap): the key never evaluates the cap twice."""

    def __init__(self, state: str, event_id: str, digest: str) -> None:
        super().__init__(state)
        self.state = state
        self.event_id = event_id
        self.digest = digest


def _intended_patch(intent: TransitionIntent) -> dict[str, object]:
    """The minimal canonical state-mutation patch persisted with the intent."""
    patch: dict[str, object] = {
        "state": intent.target_state,
        "version": intent.expected_version + 1,
        "updated_at": intent.updated_at,
        "updated_epoch": intent.updated_epoch,
    }
    if intent.superseded_by is not None:
        patch["superseded_by"] = intent.superseded_by
    if intent.supersedes:
        patch["supersedes"] = list(intent.supersedes)
    if intent.merged_from:
        patch["merged_from"] = list(intent.merged_from)
    if intent.contradicts:
        patch["contradicts"] = list(intent.contradicts)
    if intent.promoted_to is not None:
        patch["promoted_to"] = intent.promoted_to
    if intent.promoted_at is not None:
        patch["promoted_at"] = intent.promoted_at
    return patch


def _canonical_patch_sha(patch: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(patch, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


#: Private, fail-closed collection -> object_type mapping (Yua S3 micro-ruling). Derived from the
#: canonical mapping (``transitions.py::_COLLECTION_TO_OBJECT_TYPE``) but NOT imported from it — S3
#: must not touch the S7 seam. A parity/coverage test pins these against the canonical source; an
#: unknown collection fails closed (a pre-mutation terminal validation error).
_COLLECTION_TO_OBJECT_TYPE: dict[str, str] = {
    "musubi_episodic": "episodic",
    "musubi_curated": "curated",
    "musubi_concept": "concept",
    "musubi_artifact": "artifact",
    "musubi_thought": "thought",
}


class _TerminalValidation(Exception):
    """A pre-mutation validation/invariant failure (unknown collection, missing object, illegal
    transition). ``transition()`` maps it to a typed terminal ``Err`` and marks the durable row
    ABANDONED — the Qdrant mutation is never attempted (Yua S3 correction 6)."""


def _object_type_for_collection(collection: str) -> str:
    """The canonical object_type for a Qdrant collection, fail-closed on an unknown collection."""
    object_type = _COLLECTION_TO_OBJECT_TYPE.get(collection)
    if object_type is None:
        raise _TerminalValidation(f"unknown collection {collection!r}: cannot derive object_type")
    return object_type


class LifecycleTransitionCoordinator:
    """Durable-intent transition coordinator (S2-S6 admission/apply/reconcile/maintenance). One
    instance per process owns the connection policy and transition logic against the shared
    lifecycle SQLite DB and the injected Qdrant client."""

    def __init__(
        self,
        *,
        client: Any = None,
        db_path: Path,
        pending_cap: int = DEFAULT_PENDING_CAP,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX,
        busy_timeout_ms: int = store.DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._pending_cap = _validate_pending_cap(pending_cap)
        self._lease_ttl = _validate_lease_ttl(lease_ttl)
        self._backoff_base = _validate_positive_float("backoff_base_s", backoff_base_s)
        self._backoff_max = _validate_positive_float("backoff_max_s", backoff_max_s)
        if self._backoff_max < self._backoff_base:
            raise ValueError(
                f"backoff_max_s ({self._backoff_max}) must be >= backoff_base_s ({self._backoff_base})"
            )
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms < 0
        ):
            raise ValueError(
                f"busy_timeout_ms must be a non-negative int (not bool), got {busy_timeout_ms!r}"
            )
        self._busy_timeout_ms = busy_timeout_ms
        self._client = client
        self._db = Path(db_path)
        # S6 rollback barrier: one stable inode beside the lifecycle DB. Every transition and
        # reconcile pass holds LOCK_SH for its full operation; rollback drains them with LOCK_EX.
        self._maintlock = str(self._db) + ".maintlock"
        self._deploy_handoff: Callable[[], bool] = lambda: True
        #: Private fault-injection seam (default no-op); tests set it to raise/crash at
        #: a named boundary. Not a public switch.
        self._checkpoint: Callable[[str], None] = lambda _name: None
        #: Additive intent-kind handlers (C4/ART-001): a registered handler owns the APPLY of a
        #: non-``lifecycle_transition`` intent while the coordinator keeps owning the durable
        #: intent lifecycle (admission, claim/lease, attempts/backoff, reconcile, terminal). The
        #: lifecycle-transition path is the built-in default and never consults this registry.
        self._intent_handlers: dict[str, Callable[[CustomIntentContext], str]] = {}
        conn = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            store.ensure_schema(conn)
        finally:
            conn.close()

    def _maintenance_active(self) -> bool:
        """Read the durable cross-process maintenance flag."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            row = con.execute(
                "SELECT maintenance_active FROM lifecycle_control WHERE id=1"
            ).fetchone()
            return bool(row and row[0])
        finally:
            con.close()

    def readiness_check(self) -> bool:
        """Prove shared storage/schema is open and reconciliation may participate."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            store.ensure_schema(con)
            row = con.execute(
                "SELECT maintenance_active FROM lifecycle_control WHERE id=1"
            ).fetchone()
            return row is not None and not bool(row[0])
        finally:
            con.close()

    def _set_maintenance(self, active: bool, *, bump_generation: bool) -> int:
        """Durably set maintenance state and return its current generation."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            if bump_generation:
                con.execute(
                    "UPDATE lifecycle_control SET maintenance_active=?, generation=generation+1 "
                    "WHERE id=1",
                    (int(active),),
                )
            else:
                con.execute(
                    "UPDATE lifecycle_control SET maintenance_active=? WHERE id=1", (int(active),)
                )
            row = con.execute("SELECT generation FROM lifecycle_control WHERE id=1").fetchone()
            con.execute("COMMIT")
            if row is None:
                raise store.LifecycleStoreError("lifecycle_control singleton is missing")
            return int(row[0])
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @contextmanager
    def _barrier_admit(self, *, role: str) -> Iterator[bool | None]:
        """Hold a shared flock for a full transition/reconcile operation.

        Maintenance is checked only after acquiring the stable shared lock. This closes the
        check-to-lock race: a rollback first sets the durable flag, then waits for LOCK_EX; old
        holders drain, and new holders acquire LOCK_SH only after cutover and refuse while active.
        """
        if role not in ("admission", "reconcile"):
            raise ValueError(f"unknown maintenance-barrier role: {role!r}")
        fd = os.open(self._maintlock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            self._checkpoint("shared_lease_acquired")
            try:
                maintenance_active = self._maintenance_active()
            except (sqlite3.Error, store.LifecycleStoreError):
                # Fail closed without entering the mutation/reconcile body. Admission maps this
                # sentinel to its established durable_begin_failed outcome; reconcile is a no-op.
                yield None
                return
            if maintenance_active:
                yield False
                return
            yield True
            self._checkpoint("before_shared_release")
        finally:
            os.close(fd)

    def _count_nonterminal(self, con: sqlite3.Connection | None = None) -> int:
        """Count PENDING/APPLIED rows, optionally inside a caller's write transaction."""
        owned = con is None
        if con is None:
            con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            if owned:
                con.close()

    def rollback(self, *, expected_generation: int) -> Ok[RollbackDone] | Err[TransitionError]:
        """Reverse the outbox schema only after durably quiescing and draining all readers.

        A refused rollback intentionally leaves maintenance active. Operators must call
        :meth:`abort_maintenance` with the current generation to resume safely.
        """
        new_generation = self._set_maintenance(True, bump_generation=True)
        fd = os.open(self._maintlock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._checkpoint("rollback_pre_lock")
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._checkpoint("ex_acquired")
            con = store.connect(
                self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None
            )
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT generation FROM lifecycle_control WHERE id=1").fetchone()
                current_generation = int(row[0]) if row else -1
                if expected_generation != new_generation or current_generation != new_generation:
                    con.execute("COMMIT")
                    return Err(error=TransitionError(code="rollback_refused_stale_generation"))
                if self._count_nonterminal(con) > 0:
                    con.execute("COMMIT")
                    return Err(error=TransitionError(code="rollback_refused_nonterminal"))
                self._checkpoint("rollback_before_drop")
                con.execute("DROP TABLE lifecycle_outbox")
                con.execute("COMMIT")
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
            if not self._deploy_handoff():
                return Err(error=TransitionError(code="handoff_failed"))
            self._set_maintenance(False, bump_generation=False)
            return Ok(value=RollbackDone(generation=new_generation))
        finally:
            os.close(fd)

    def abort_maintenance(
        self, *, expected_generation: int
    ) -> Ok[RollbackDone] | Err[TransitionError]:
        """Explicitly resume a refused/failed maintenance window under its generation fence."""
        fd = os.open(self._maintlock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            con = store.connect(
                self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None
            )
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT generation FROM lifecycle_control WHERE id=1").fetchone()
                current_generation = int(row[0]) if row else -1
                if current_generation != expected_generation:
                    con.execute("COMMIT")
                    return Err(error=TransitionError(code="abort_refused_stale_generation"))
                con.execute("UPDATE lifecycle_control SET maintenance_active=0 WHERE id=1")
                con.execute("COMMIT")
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
            return Ok(value=RollbackDone(generation=expected_generation, kind="aborted"))
        finally:
            os.close(fd)

    def backfill_terminal_epoch(self) -> int:
        """Backfill known terminal ages from persisted patch JSON; preserve unknown ages as NULL."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        updated = 0
        try:
            rows = con.execute(
                "SELECT operation_key, patch_json FROM lifecycle_outbox "
                "WHERE state IN ('FINAL','ABANDONED') AND terminal_epoch IS NULL"
            ).fetchall()
            for operation_key, patch_json in rows:
                try:
                    age = json.loads(patch_json).get("updated_epoch") if patch_json else None
                except (TypeError, ValueError, json.JSONDecodeError):
                    age = None
                if (
                    isinstance(age, (int, float))
                    and not isinstance(age, bool)
                    and math.isfinite(float(age))
                ):
                    con.execute(
                        "UPDATE lifecycle_outbox SET terminal_epoch=? WHERE operation_key=?",
                        (float(age), operation_key),
                    )
                    updated += 1
            con.commit()
            return updated
        finally:
            con.close()

    def cleanup_terminal(self, *, cutoff_epoch: object, batch_limit: object) -> CleanupReport:
        """Delete one deterministic bounded batch of old terminal outbox rows atomically."""
        if (
            isinstance(cutoff_epoch, bool)
            or not isinstance(cutoff_epoch, (int, float))
            or not math.isfinite(float(cutoff_epoch))
            or cutoff_epoch <= 0
        ):
            raise CleanupConfigError(
                f"cutoff_epoch must be a positive finite number, got {cutoff_epoch!r}"
            )
        if isinstance(batch_limit, bool) or not isinstance(batch_limit, int) or batch_limit <= 0:
            raise CleanupConfigError(f"batch_limit must be a positive int, got {batch_limit!r}")
        sql = (
            "WITH sel AS (SELECT operation_key FROM lifecycle_outbox WHERE state IN "
            "('FINAL','ABANDONED') AND terminal_epoch IS NOT NULL AND "
            "terminal_epoch < :cutoff ORDER BY terminal_epoch, operation_key LIMIT :batch) "
            "DELETE FROM lifecycle_outbox WHERE operation_key IN "
            "(SELECT operation_key FROM sel) AND state IN ('FINAL','ABANDONED') AND "
            "terminal_epoch IS NOT NULL AND terminal_epoch < :cutoff RETURNING operation_key"
        )
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            deleted = len(
                con.execute(sql, {"cutoff": float(cutoff_epoch), "batch": batch_limit}).fetchall()
            )
            remaining_row = con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED') "
                "AND terminal_epoch IS NOT NULL AND terminal_epoch < ?",
                (float(cutoff_epoch),),
            ).fetchone()
            total_row = con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('FINAL','ABANDONED')"
            ).fetchone()
            con.execute("COMMIT")
            return CleanupReport(
                deleted=deleted,
                remaining_eligible=int(remaining_row[0]) if remaining_row else 0,
                terminal_total=int(total_row[0]) if total_row else 0,
            )
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def transition(self, intent: TransitionIntent) -> Ok[TransitionOutcome] | Err[TransitionError]:
        """Run one full transition while holding the S6 shared maintenance barrier."""
        with self._barrier_admit(role="admission") as admitted:
            if admitted is None:
                return Err(error=TransitionError(code="durable_begin_failed"))
            if not admitted:
                return Err(error=TransitionError(code="maintenance_active"))
            return self._transition_locked(intent)

    def _transition_locked(
        self, intent: TransitionIntent
    ) -> Ok[TransitionOutcome] | Err[TransitionError]:
        """Full lifecycle transition (S3): resolve operation_key idempotency, admit a durable
        PENDING row, persist a canonical lifecycle event BEFORE any mutation, conditionally apply
        the version-fenced mutation with a full readback, then atomically finalize.

        Returns ``Ok(TransitionFinal)`` on a confirmed apply + finalize; ``Ok(TransitionPending)``
        on a recoverable outcome (corrupt readback, or a transient/unknown apply failure) left for
        the S4 reconciler; or a bounded ``Err`` — ``cap_exceeded`` / ``active_intent_exists`` /
        ``durable_begin_failed`` / ``operation_key_conflict`` / ``version_fence_violation`` /
        ``terminal_apply_failure``."""
        # namespace/actor/reason are required admission truth (Yua S4): a PENDING row must be
        # self-sufficient for reconcile's server-fenced reapply + event rebuild even before its
        # event_payload exists. Reject a degenerate intent BEFORE any durable work — a bounded
        # terminal validation, no row, no mutation. (The digest already binds all three.)
        if not (intent.namespace and intent.actor and intent.reason):
            return Err(error=TransitionError(code="terminal_apply_failure"))
        opk = self._key(intent)
        digest = self._intent_digest(intent)
        event_id = generate_ksuid()
        try:
            # (1) operation_key idempotency BEFORE the cap (S3 correction 5): an existing row for
            # this key short-circuits to its recorded outcome; the SAME key with a DIFFERENT intent
            # digest is a conflict. No cap gate, no mutation, no new row/event.
            replay = self._replay(opk, digest)
            if replay is not None:
                return replay
            # (2) durable admission: cap gate + single-active + PENDING row, atomically. No Qdrant.
            self._write_pending(intent, opk, event_id)
        except _AlreadyExists as exc:
            # the SERIALIZED admission re-check found a concurrent winner's row (our out-of-txn
            # _replay missed it) — resolve idempotency BEFORE the cap, exactly like _replay.
            return self._resolve_existing(opk, exc.state, exc.event_id, exc.digest, digest)
        except _CapExceeded:
            return Err(error=TransitionError(code="cap_exceeded"))
        except sqlite3.IntegrityError as exc:
            errorcode = getattr(exc, "sqlite_errorcode", None)
            # ONLY the ux_active_intent partial-unique (collection, object_id) violation is
            # active_intent_exists (SQLITE_CONSTRAINT_UNIQUE).
            if errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                return Err(error=TransitionError(code="active_intent_exists"))
            # operation_key PRIMARY KEY collision: a concurrent caller with our EXACT key won the
            # insert race (replay-before-insert is check-then-insert). Re-resolve AFTER the winner
            # committed — the same digest replays its stable outcome, a different digest is an
            # operation_key_conflict; only then does an unrelated constraint fall through to a
            # generic durable-begin failure (S3 integrity: concurrent same-key idempotency).
            if errorcode == sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY:
                resolved = self._replay(opk, digest)
                if resolved is not None:
                    return resolved
                return Err(error=TransitionError(code="durable_begin_failed"))
            return Err(error=TransitionError(code="durable_begin_failed"))
        except (sqlite3.Error, store.LifecycleStoreError):
            # A durable-begin failure in EITHER the replay read OR the admission write (a SQLite
            # error, or a store.connect that could not establish the WAL policy — LifecycleStoreError
            # is a RuntimeError, named explicitly). No row, no mutation.
            return Err(error=TransitionError(code="durable_begin_failed"))
        # The durable PENDING row exists and Qdrant is untouched: the admission/mutation race +
        # crash seam. The two-process race barrier pauses the winner HERE (post-admission,
        # pre-Qdrant) so a rejected loser makes zero Qdrant calls. A fault here propagates — it is
        # OUTSIDE the durable-begin catch and must NEVER map to durable_begin_failed (the row is
        # committed).
        self._checkpoint("after_pending_commit")
        # (3) persist the canonical lifecycle event on the outbox BEFORE any mutation (S3
        # correction 1): derived from an exact pre-apply read so a post-Qdrant crash finalizes from
        # the stored payload with no fabricated from_state/actor/reason. A pre-mutation validation
        # failure (unknown collection, missing object, illegal transition) is terminal.
        try:
            self._persist_event(intent, opk, event_id)
        except _TerminalValidation:
            self._mark_terminal(opk)
            return Err(error=TransitionError(code="terminal_apply_failure"))
        # (4) conditional apply: server-side fenced mutation + full readback + confirm.
        try:
            status = self._apply_conditional(
                opk, intent.collection, intent.object_id, _intended_patch(intent)
            )
        except Exception as exc:  # classified into terminal vs recoverable next
            if self._classify_terminal(exc):
                self._mark_terminal(opk)
                return Err(error=TransitionError(code="terminal_apply_failure"))
            # transport/unknown failure -> keep PENDING for the S4 reconciler (correction 6).
            return Ok(value=TransitionPending(operation_key=opk, event_id=event_id))
        if status == "fence":
            # a known version fence is terminal (the intent is stale) — abandon, never retry.
            self._mark_terminal(opk)
            return Err(error=TransitionError(code="version_fence_violation"))
        if status == "corrupt":
            # the version landed but a deeper patch key mismatched — recoverable; S4 reconciles.
            return Ok(value=TransitionPending(operation_key=opk, event_id=event_id))
        # (5) confirmed: the Qdrant mutation is durable but the row is still PENDING here — the
        # crash seam a reconcile recovers by readback (R6/R17). The effective-apply marker +
        # PENDING->APPLIED then commit TOGETHER (correction 3), so a crash can never leave an APPLIED
        # row without its marker (or vice versa).
        self._checkpoint("after_qdrant_readback_before_applied_commit")
        self._mark_applied(opk, intent.object_id, intent.target_state)
        self._checkpoint("after_applied_commit_before_finalize")
        # (6) atomic finalize: the 8-column FINAL lifecycle_events row (from the persisted payload)
        # AND the APPLIED->FINAL move in ONE transaction, guarded on state='APPLIED' (R8). A fault
        # INSIDE finalize rolls the whole txn back; the mutation is ALREADY durable (APPLIED), so the
        # row stays APPLIED and the S4 reconciler completes it to FINAL. Return Pending (never Final)
        # so a caller cannot observe Final for an un-finalized op.
        try:
            self._finalize(opk)
        except Exception:
            return Ok(value=TransitionPending(operation_key=opk, event_id=event_id))
        return Ok(value=TransitionFinal(operation_key=opk, event_id=event_id))

    # -- internals ------------------------------------------------------------------ #

    def _key(self, intent: TransitionIntent) -> str:
        """The stable canonical operation key when the intent supplies none."""
        return (
            intent.operation_key
            or f"canon:{intent.collection}:{intent.object_id}:"
            f"{intent.expected_version}:{intent.target_state}"
        )

    def _intent_digest(self, intent: TransitionIntent) -> str:
        fields: dict[str, object] = {
            "collection": intent.collection,
            "object_id": intent.object_id,
            "namespace": intent.namespace,
            "expected_version": intent.expected_version,
            "target_state": intent.target_state,
            "actor": intent.actor,
            "reason": intent.reason,
            "superseded_by": intent.superseded_by,
            "supersedes": list(intent.supersedes),
            "merged_from": list(intent.merged_from),
            "contradicts": list(intent.contradicts),
            "promoted_to": intent.promoted_to,
            "promoted_at": intent.promoted_at,
        }
        canon = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()

    def _over_cap(self, con: sqlite3.Connection) -> bool:
        count = con.execute(
            "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
        ).fetchone()[0]
        return bool(count >= self._pending_cap)

    def _write_pending(
        self, intent: TransitionIntent, opk: str, event_id: str, state: str = "PENDING"
    ) -> None:
        """Atomic admission: ``BEGIN IMMEDIATE`` → count non-terminal globally → cap
        gate → ``INSERT`` → ``COMMIT`` in one write transaction so concurrent admissions
        serialize on the write lock. At/over the cap: raise ``_CapExceeded`` and write no
        row. The ``INSERT`` is not ``OR IGNORE`` — a partial-unique-index violation (a
        second active intent for the object) raises ``IntegrityError`` so the loser is
        rejected."""
        self._checkpoint("before_pending_commit")
        patch = _intended_patch(intent)
        patch_sha = _canonical_patch_sha(patch)
        patch_json = json.dumps(patch, sort_keys=True, separators=(",", ":"))
        params = (
            opk,
            intent.object_id,
            intent.collection,
            intent.target_state,
            intent.expected_version,
            intent.namespace,
            intent.actor,
            intent.reason,
            patch_sha,
            patch_json,
            self._intent_digest(intent),
            state,
            event_id,
        )
        insert = (
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
            "expected_version,namespace,actor,reason,patch_sha,patch_json,intent_digest,state,"
            "event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                # SERIALIZED operation_key re-check (closes the same-key/full-cap TOCTOU): inside the
                # write lock, a row already existing for this key means a concurrent winner committed
                # AFTER our out-of-transaction _replay. Resolve idempotency BEFORE the cap so the key
                # never evaluates the cap twice. Same connection — no second txn while holding this one.
                prior = con.execute(
                    "SELECT state, event_id, intent_digest FROM lifecycle_outbox "
                    "WHERE operation_key=?",
                    (opk,),
                ).fetchone()
                if prior is not None:
                    con.execute("ROLLBACK")
                    raise _AlreadyExists(state=prior[0], event_id=prior[1], digest=prior[2])
                if self._over_cap(con):
                    con.execute("ROLLBACK")
                    raise _CapExceeded()
                con.execute(insert, params)
                con.execute("COMMIT")
            except sqlite3.IntegrityError:
                con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        # NOTE: the after_pending_commit checkpoint is fired by transition(), OUTSIDE the
        # durable-begin catch — a post-commit fault must not map to durable_begin_failed.

    # -- S3: idempotent replay ------------------------------------------------------- #

    def _row_for_key(self, opk: str) -> tuple[str, str, str] | None:
        """``(state, event_id, intent_digest)`` for an existing outbox row, or ``None``."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            row = con.execute(
                "SELECT state, event_id, intent_digest FROM lifecycle_outbox WHERE operation_key=?",
                (opk,),
            ).fetchone()
        finally:
            con.close()
        return (row[0], row[1], row[2]) if row else None

    def _resolve_existing(
        self, opk: str, state: str, event_id: str, stored_digest: str, digest: str
    ) -> Ok[TransitionOutcome] | Err[TransitionError]:
        """Idempotency resolution shared by the out-of-transaction ``_replay`` fast path and the
        in-transaction TOCTOU re-check: the SAME digest replays the recorded outcome with its stable
        event_id (FINAL→Final, ABANDONED→terminal Err, else Pending); a DIFFERENT digest is an
        ``operation_key_conflict``. No mutation, no new row/event."""
        if stored_digest != digest:
            return Err(error=TransitionError(code="operation_key_conflict"))
        if state == "FINAL":
            return Ok(value=TransitionFinal(operation_key=opk, event_id=event_id))
        if state == "ABANDONED":
            return Err(error=TransitionError(code="terminal_apply_failure"))
        return Ok(value=TransitionPending(operation_key=opk, event_id=event_id))

    def _replay(self, opk: str, digest: str) -> Ok[TransitionOutcome] | Err[TransitionError] | None:
        """operation_key idempotency FAST path, resolved BEFORE the cap (S3 correction 5). An
        existing row short-circuits to its recorded outcome; ``None`` when the key is new so the
        caller falls through to admission, where the SERIALIZED re-check closes the TOCTOU against a
        concurrent winner whose row this out-of-transaction read may have missed."""
        existing = self._row_for_key(opk)
        if existing is None:
            return None
        return self._resolve_existing(opk, existing[0], existing[1], existing[2], digest)

    # -- S3: pre-apply read + persisted canonical event ------------------------------ #

    def _require_client(self) -> Any:
        if self._client is None:
            raise _TerminalValidation("no Qdrant client injected: cannot read or apply")
        return self._client

    def _read_object(
        self, collection: str, object_id: str, namespace: str
    ) -> tuple[dict[str, Any], int]:
        """Read the object's AUTHORITATIVE identity payload, requesting enough rows to PROVE exactly one
        match for ``object_id`` within ``namespace`` (returns ``(payload, count)``; empty payload if
        none).

        DATA-001 P2: excludes write-once CONTENT snapshots (``point_kind == "content"``) so a v2 object
        (anchor + one-or-more content points) still reads as EXACTLY ONE identity row — the anchor (full
        mutable state/version) — instead of count==2. A v1/legacy row (no ``point_kind``) and every
        concept/thought/artifact row (no content points) are unaffected: the exclusion is a no-op there.
        Without this, every durable lifecycle transition on a reinforced/updated episodic or curated
        object would fence/abandon on the count check."""
        points, _ = self._require_client().scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                ],
                must_not=[
                    models.FieldCondition(
                        key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                    )
                ],
            ),
            limit=2,
            with_payload=True,
        )
        payload = dict(points[0].payload or {}) if points else {}
        return payload, len(points)

    def _persist_event(self, intent: TransitionIntent, opk: str, event_id: str) -> None:
        """Build the canonical :class:`LifecycleEvent` from an exact pre-apply read and persist its
        ``model_dump_json`` on the outbox row BEFORE any Qdrant mutation (S3 correction 1), so a
        post-Qdrant crash finalizes from the stored payload without fabricating
        ``from_state``/``actor``/``reason``. A pre-mutation invariant failure (unknown collection,
        missing/ambiguous object, illegal transition) raises :class:`_TerminalValidation`."""
        object_type = _object_type_for_collection(intent.collection)
        current, count = self._read_object(intent.collection, intent.object_id, intent.namespace)
        if count != 1:
            raise _TerminalValidation(
                f"pre-apply read for {intent.object_id} in "
                f"{intent.collection}/{intent.namespace} matched {count} points (need exactly 1)"
            )
        from_state = current.get("state")
        if not isinstance(from_state, str):
            raise _TerminalValidation(f"object {intent.object_id} has no readable lifecycle state")
        try:
            lineage_changes = {
                key: value
                for key, value in _intended_patch(intent).items()
                if key
                in {
                    "superseded_by",
                    "supersedes",
                    "merged_from",
                    "contradicts",
                    "promoted_to",
                    "promoted_at",
                }
            }
            # model_validate over a dict: the object_type/from_state/to_state come from runtime
            # data (mapping + readback + intent) as plain strings; pydantic validates them against
            # the ObjectType/LifecycleState literals at runtime and the legal-transition rule.
            event = LifecycleEvent.model_validate(
                {
                    "event_id": event_id,
                    "object_id": intent.object_id,
                    "object_type": object_type,
                    "namespace": intent.namespace,
                    "from_state": from_state,
                    "to_state": intent.target_state,
                    "actor": intent.actor,
                    "reason": intent.reason,
                    "lineage_changes": lineage_changes,
                }
            )
        except (ValidationError, ValueError) as exc:
            # illegal transition / invalid field — a pre-mutation invariant failure -> terminal.
            raise _TerminalValidation(str(exc)) from exc
        payload_json = event.model_dump_json()
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            cur = con.execute(
                "UPDATE lifecycle_outbox SET event_payload=? "
                "WHERE operation_key=? AND state='PENDING'",
                (payload_json, opk),
            )
            con.commit()
        finally:
            con.close()
        if cur.rowcount != 1:
            # zero PENDING rows means the row vanished/changed under us: fail pre-mutation and
            # NEVER proceed to Qdrant after a no-op event persist (S3 integrity hole 1).
            raise _TerminalValidation(
                f"persist-event matched {cur.rowcount} PENDING rows for {opk} (need exactly 1)"
            )

    # -- S3: conditional apply + full readback confirm ------------------------------- #

    def _namespace_for(self, opk: str) -> str:
        """The object's namespace from the admission-truth outbox column (Option A), so a fenced
        reapply resolves namespace WITHOUT a live intent (S4 reconcile / white-box callers)."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            row = con.execute(
                "SELECT namespace FROM lifecycle_outbox WHERE operation_key=?", (opk,)
            ).fetchone()
        finally:
            con.close()
        return str(row[0]) if row and row[0] else ""

    def _apply_conditional(
        self, opk: str, collection: str, object_id: str, patch: dict[str, object]
    ) -> str:
        """Send the EXACT patch fenced server-side (collection + object_id + namespace +
        expected_version), then FULL-readback and confirm (S3 correction 4). ``namespace`` is
        resolved from the stored admission truth (Option A) so this works for a live transition AND a
        reconcile with no live intent. Returns ``'confirmed'`` | ``'fence'`` | ``'corrupt'``; the
        fenced ``set_payload`` matches zero points when the object is not at ``expected_version`` (a
        stale intent) -> the readback proves a fence."""
        namespace = self._namespace_for(opk)
        expected_version = int(str(patch["version"])) - 1
        client = self._require_client()
        client.set_payload(
            collection_name=collection,
            payload=dict(patch),
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="version", match=models.MatchValue(value=expected_version)
                    ),
                ]
            ),
        )
        actual, count = self._read_object(collection, object_id, namespace)
        return self._confirm(patch, object_id, namespace, actual, count)

    def _confirm(
        self,
        patch: dict[str, object],
        object_id: str,
        namespace: str,
        actual: dict[str, Any],
        count: int,
    ) -> str:
        """Confirm an apply from the ACTUAL readback (S3 correction 4). ``'fence'`` = stale /
        not-exactly-one / wrong identity-version-state (the fenced write matched zero points);
        ``'corrupt'`` = the version+state landed but an intended patch key is missing/mismatched
        (recoverable); ``'confirmed'`` = exactly one point, identity + namespace + version + state
        all correct, every intended key present, and the SHA over the ACTUAL-projected patch equals
        the intended SHA."""
        target_state = patch["state"]
        expected_version = int(str(patch["version"])) - 1
        if count != 1:
            return "fence"
        if str(actual.get("namespace", "")) != namespace:
            return "fence"
        actual_object_id = actual.get("object_id")
        if actual_object_id is not None and str(actual_object_id) != object_id:
            return "fence"
        if actual.get("version") != expected_version + 1:
            return "fence"
        if actual.get("state") != target_state:
            return "fence"
        for key in patch:
            if key not in actual:
                return "corrupt"
        projected = {key: actual[key] for key in patch}
        if _canonical_patch_sha(projected) != _canonical_patch_sha(patch):
            return "corrupt"
        return "confirmed"

    # -- S3: marker + APPLIED (one txn) and atomic finalize -------------------------- #

    def _mark_applied(
        self, opk: str, object_id: str, target_state: str, owner: str | None = None
    ) -> None:
        """The confirmed effective-apply marker AND the PENDING->APPLIED move commit TOGETHER (S3
        correction 3). A marker key collision must verify identical object/target — never silently
        hide a mismatch. When ``owner`` is given (S4 reconcile), the APPLIED move is owner-guarded so
        only the current lease holder advances the row."""
        guard, gparams = self._owner_guard(owner)
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Whether a MATCHING apply marker already existed BEFORE this txn. A benign replay of a
            # terminal row requires this: marker+APPLIED are written atomically (S3 correction 3), so
            # a terminal row whose marker we had to freshly insert is corruption, not a replay.
            marker_preexisted = False
            try:
                con.execute(
                    "INSERT INTO lifecycle_apply_markers "
                    "(operation_key, object_id, target_state) VALUES (?,?,?)",
                    (opk, object_id, target_state),
                )
            except sqlite3.IntegrityError:
                existing = con.execute(
                    "SELECT object_id, target_state FROM lifecycle_apply_markers "
                    "WHERE operation_key=?",
                    (opk,),
                ).fetchone()
                if existing is None or existing[0] != object_id or existing[1] != target_state:
                    # a genuine marker mismatch — surfaced (the single outer handler rolls back).
                    raise
                # identical marker already present (idempotent re-apply) — fall through.
                marker_preexisted = True
            cur = con.execute(
                f"UPDATE lifecycle_outbox SET state='APPLIED' "
                f"WHERE operation_key=? AND state='PENDING'{guard}",
                (opk, *gparams),
            )
            if cur.rowcount == 1:
                # the marker + APPLIED moved EXACTLY one PENDING row (the normal first apply).
                con.execute("COMMIT")
                return
            if cur.rowcount > 1:
                # operation_key is the PRIMARY KEY, so this cannot happen — but never let an apply
                # marker orphan (committed with no APPLIED row); refuse (S3 integrity hole 2).
                raise RuntimeError(
                    f"mark-applied matched {cur.rowcount} PENDING rows for {opk} (need exactly 1)"
                )
            # rowcount == 0: the PENDING->APPLIED move matched nothing. This is EITHER a benign
            # idempotent REPLAY — the row was already advanced to APPLIED/FINAL by an earlier pass
            # or the winner of a two-connection race, the exact LifecycleJobCrashed signature — OR a
            # real fault. Read the row back and discriminate; only a proven-applied, identity-matched
            # row returns idempotently, everything else fails loud. The transaction (including any
            # marker inserted above) is rolled back on this path, so a benign replay mutates nothing.
            row = con.execute(
                "SELECT state, object_id, target_state FROM lifecycle_outbox WHERE operation_key=?",
                (opk,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"mark-applied: no lifecycle_outbox row for {opk}")
            row_state, row_oid, row_tstate = row[0], row[1], row[2]
            if row_oid != object_id or row_tstate != target_state:
                # the row exists but is a DIFFERENT operation identity — a genuine mismatch, not a
                # replay of THIS apply. Surface it (never silently converge onto the wrong row).
                raise RuntimeError(
                    f"mark-applied identity mismatch for {opk}: "
                    f"row=({row_oid!r},{row_tstate!r}) call=({object_id!r},{target_state!r})"
                )
            if row_state in ("APPLIED", "FINAL"):
                if not marker_preexisted:
                    # a terminal row whose apply marker did NOT already exist: the atomic
                    # marker+APPLIED invariant (S3 correction 3) is broken — this is corruption,
                    # not a benign replay. Fail loud; the marker we inserted this txn is rolled
                    # back by the outer handler, so we mutate nothing.
                    raise RuntimeError(
                        f"mark-applied corruption for {opk}: row is {row_state} but had NO "
                        f"pre-existing apply marker (marker+APPLIED must be atomic)"
                    )
                # already applied, matching identity, AND a pre-existing matching marker — a valid
                # idempotent replay. Roll back (no mutation) and return normally.
                con.execute("ROLLBACK")
                return
            if row_state == "PENDING":
                # still PENDING yet the guarded UPDATE excluded it: a genuine lease-owner mismatch
                # (S4) — some other lease holder owns this row. Fail loud; do NOT steal the apply.
                raise RuntimeError(
                    f"mark-applied lease mismatch for {opk}: row is PENDING under a different owner"
                )
            # ABANDONED or any other state: mark-applied is not a legal transition from here.
            raise RuntimeError(f"mark-applied: unexpected state {row_state!r} for {opk}")
        except Exception:
            # ONE rollback owner (S3 integrity hole 3): guard on in_transaction so a failure that
            # already ended the txn is not masked by a second "no transaction" rollback error.
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _finalize(
        self,
        opk: str,
        event_id: str | None = None,
        object_id: str | None = None,
        namespace: str | None = None,
        target_state: str | None = None,
        *,
        owner: str | None = None,
    ) -> None:
        """Atomic FINAL (S3 correction 1, R8 forward guard): move APPLIED->FINAL (stamping
        ``terminal_epoch``) AND insert the 8-column FINAL ``lifecycle_events`` row FROM the persisted
        event payload in ONE transaction. When ``owner`` is given (S4 reconcile) the FINAL move is
        owner-guarded and clears the lease atomically; a NON-owner (or non-APPLIED) finalize matches
        zero rows and is a SILENT NO-OP — exact-owner semantics (R16): never a raise, never an event,
        no second effective apply. The ``owner=None`` path keeps the strict R8 forward guard (raise on
        rowcount != 1). A replayed ``event_id`` must be byte-identical to the stored payload — a
        conflicting collision is surfaced. The ``event_id``/``object_id``/``namespace``/
        ``target_state`` params exist for the accepted white-box callers; the persisted payload
        (namespace/actor/reason/from_state) is authoritative."""
        guard, gparams = self._owner_guard(owner)
        release = ", lease_owner=NULL, lease_expires_epoch=NULL" if owner is not None else ""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                # The owner-guarded FINAL move FIRST: a non-owner / non-APPLIED row matches zero and
                # returns without touching Qdrant or the event (exact-owner no-op).
                cur = con.execute(
                    f"UPDATE lifecycle_outbox SET state='FINAL', terminal_epoch=?{release} "
                    f"WHERE operation_key=? AND state='APPLIED'{guard}",
                    (self._now(), opk, *gparams),
                )
                if cur.rowcount != 1:
                    con.execute("ROLLBACK")
                    if owner is None:
                        raise RuntimeError(
                            f"finalize guard: expected exactly one APPLIED row for {opk}, "
                            f"updated {cur.rowcount}"
                        )
                    return  # exact-owner: a non-owner / non-APPLIED FINAL is a silent no-op
                # The row is FINAL in-txn; write the audit event from the persisted payload.
                row = con.execute(
                    "SELECT event_payload FROM lifecycle_outbox WHERE operation_key=?", (opk,)
                ).fetchone()
                if row is None or not row[0]:
                    raise RuntimeError(f"finalize: no persisted event payload for {opk}")
                payload_json = str(row[0])
                event = LifecycleEvent.model_validate_json(payload_json)
                try:
                    con.execute(
                        "INSERT INTO lifecycle_events (event_id, object_id, namespace, object_type,"
                        " from_state, to_state, occurred_epoch, payload) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            event.event_id,
                            event.object_id,
                            event.namespace,
                            event.object_type,
                            event.from_state,
                            event.to_state,
                            event.occurred_epoch,
                            payload_json,
                        ),
                    )
                except sqlite3.IntegrityError:
                    existing = con.execute(
                        "SELECT payload FROM lifecycle_events WHERE event_id=?", (event.event_id,)
                    ).fetchone()
                    if existing is None or existing[0] != payload_json:
                        # a conflicting event collision — surfaced, never silently accepted.
                        raise
                    # identical event (idempotent re-finalize) — proceed.
                # R8 crash seam: a fault here rolls back BOTH the FINAL move and the event insert,
                # leaving the row EXACTLY APPLIED (never a half-finalized FINAL with no event).
                self._checkpoint("inside_finalize_after_event_insert")
                con.execute("COMMIT")
            except Exception:
                # ONE rollback owner (S3 integrity hole 3): guard on in_transaction so the original
                # failure is preserved, never masked by a redundant "no transaction" rollback.
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    # -- S3: classification + terminal marking --------------------------------------- #

    def _classify_terminal(self, exc: Exception) -> bool:
        """A KNOWN-terminal apply failure -> abandon; transport/unknown -> transient (keep PENDING,
        never abandoned by uncertainty); a pre-mutation invariant failure is terminal (S3
        correction 6). Delegates to the 3-way :meth:`_classify`."""
        return self._classify(exc) == "terminal"

    def _drive_custom_intent(
        self,
        kind: str,
        opk: str,
        oid: str,
        coll: str,
        ns: str,
        token: str,
        counts: dict[str, int],
        patch_json: str | None = None,
    ) -> None:
        """Drive a claimed non-transition intent (C4/ART-001): dispatch to the registered handler and
        map its outcome onto the SAME disposition policy as a transition apply. A missing handler keeps
        the row PENDING + backoff (a not-yet-registered handler is transient, never abandoned). The
        lease releases at every terminal/pending disposition; the confirmed path holds the lease into an
        owner-guarded ``_finalize_custom``."""
        handler = self._intent_handlers.get(kind)
        if handler is None:
            self._persist_attempt(
                opk, reschedule=True, failure_class="transient", owner=token, release=True
            )
            counts["pending"] += 1
            return
        ctx = CustomIntentContext(
            operation_key=opk,
            object_id=oid,
            collection=coll,
            namespace=ns,
            owner_token=token,
            patch_json=patch_json,
        )
        try:
            outcome = handler(ctx)
        except Exception as exc:  # same terminal-vs-transient classification as a transition apply
            cls = self._classify(exc)
            self._observe_failure(cls)
            if cls == "terminal":
                self._persist_attempt(
                    opk,
                    reschedule=False,
                    state="ABANDONED",
                    failure_class="terminal",
                    owner=token,
                    release=True,
                )
                counts["abandoned"] += 1
            else:
                self._persist_attempt(
                    opk, reschedule=True, failure_class=cls, owner=token, release=True
                )
                counts["pending"] += 1
            return
        if outcome == "confirmed":
            self._persist_attempt(opk, reschedule=False, owner=token)  # count attempt, hold lease
            self._finalize_custom(opk, owner=token)
            counts["finalized"] += 1
        elif outcome == "fence":
            self._persist_attempt(
                opk,
                reschedule=False,
                state="ABANDONED",
                failure_class="terminal",
                owner=token,
                release=True,
            )
            counts["abandoned"] += 1
        elif outcome == "retry":  # transient; keep PENDING + bounded backoff
            self._persist_attempt(
                opk, reschedule=True, failure_class="transient", owner=token, release=True
            )
            counts["pending"] += 1
        else:
            # A handler that returns an outcome outside the documented set
            # {'confirmed','fence','retry'} is a programming error — fail FAST/terminally (ABANDON),
            # never an infinite retry loop that masks the bug behind endless backoff.
            self._observe_failure("terminal")
            self._persist_attempt(
                opk,
                reschedule=False,
                state="ABANDONED",
                failure_class="terminal",
                owner=token,
                release=True,
            )
            counts["abandoned"] += 1

    def _finalize_custom(self, opk: str, *, owner: str) -> None:
        """Terminal FINAL for a custom intent whose handler already committed its external effect (the
        artifact head is published). Owner-guarded PENDING/APPLIED -> FINAL + lease release; NO
        ``lifecycle_events`` row (indexing is not a lifecycle transition). A non-owner is a silent
        no-op (exact-owner R16)."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE lifecycle_outbox SET state='FINAL', terminal_epoch=?, "
                "lease_owner=NULL, lease_expires_epoch=NULL "
                "WHERE operation_key=? AND state IN ('PENDING','APPLIED') AND lease_owner=?",
                (self._now(), opk, owner),
            )
            con.execute("COMMIT")
        finally:
            con.close()

    def _mark_terminal(self, opk: str) -> None:
        """Move a non-terminal row to ABANDONED, stamping ``terminal_epoch`` atomically."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute(
                "UPDATE lifecycle_outbox SET state='ABANDONED', terminal_epoch=? "
                "WHERE operation_key=? AND state IN ('PENDING','APPLIED')",
                (self._now(), opk),
            )
            con.commit()
        finally:
            con.close()

    def _now(self) -> float:
        return time.time()

    # -- S4: reconciliation — leases, attempts/backoff, crash recovery -------------- #

    def _owner_guard(self, owner: str | None) -> tuple[str, list[object]]:
        """The WHERE fragment restricting a post-claim disposition to the CURRENT lease owner (S4
        R16). ``owner=None`` (the S3 transition path) is unguarded."""
        if owner is None:
            return "", []
        return " AND lease_owner=?", [owner]

    def _new_token(self) -> str:
        """A FRESH cryptographically-strong per-claim owner token — the generation/ABA fence (R16/
        R17). Never derived from operation data, never reused: a stale owner's old token can never
        re-match a row reclaimed under a new token."""
        return secrets.token_hex(16)

    def _backoff(self, attempts: int) -> float:
        """Bounded exponential retry backoff (R15): ``min(base * 2**(attempts-1), max)``. Overflow-
        safe — the saturating exponent is derived from ``log2(max/base)`` and returned as ``max``
        beyond it, so a huge durable attempts count never evaluates an enormous ``2**n``."""
        exp = max(0, attempts - 1)
        saturating = (
            math.ceil(math.log2(self._backoff_max / self._backoff_base))
            if self._backoff_max > self._backoff_base
            else 0
        )
        if exp >= saturating:
            return self._backoff_max
        return float(min(self._backoff_base * (2**exp), self._backoff_max))

    def _classify(self, exc: Exception) -> str:
        """3-way apply-failure classification (R15): ``terminal`` (proven — abandon), ``transient``
        (known-retryable), or ``unknown`` (unclassified). An unknown is NEVER terminal — it keeps
        retrying, never abandoned by attempt count."""
        if isinstance(exc, _TerminalValidation) or getattr(exc, "terminal", False):
            return "terminal"
        if getattr(exc, "transient", False):
            return "transient"
        return "unknown"

    def _observe_failure(self, failure_class: str) -> None:
        """R19 (S5): PII-free failure observability from the reconcile apply-except. Emits a static
        event code + the bounded durable ``failure_class`` ONLY into the log (never operation_key,
        object_id, namespace, content, patch, reason, token, exception message, or traceback), and
        increments the mutation-failures counter under the bounded external ``class`` label. A durable
        ``unknown`` maps to the external ``transient`` class (there is no ``unknown`` metric label)."""
        log.warning("lifecycle_mutation_failed", extra={"failure_class": failure_class})
        metric_class = "terminal" if failure_class == "terminal" else "transient"
        _OUTBOX_MUTATION_FAILURES.labels(**{"class": metric_class}).inc()

    def _observe_pending(self) -> None:
        """R19 (S5): set the UNLABELED pending-depth gauge to the current non-terminal
        (PENDING/APPLIED) outbox depth — a bounded cap-backstop signal emitted once per reconcile
        pass. No identifier ever enters the gauge."""
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            depth = con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
            ).fetchone()[0]
        finally:
            con.close()
        _OUTBOX_PENDING.set(float(depth))

    def _cur(self, collection: str, object_id: str, namespace: str) -> tuple[object, object]:
        """The object's CURRENT ``(version, state)`` from Qdrant (readback-recovery); ``(None, None)``
        if absent/ambiguous."""
        payload, count = self._read_object(collection, object_id, namespace)
        if count != 1:
            return (None, None)
        return payload.get("version"), payload.get("state")

    def _claim(
        self, con: Any, opk: str, now: float, token: str, *, force_due: bool = False
    ) -> bool:
        """Atomic guarded lease claim (R16): ONE UPDATE (on the caller's ``con``) stamping a fresh
        token on a DUE, unleased-or-expired row. ``rowcount == 1`` IS ownership — a NON-atomic
        check-then-update would race. The FULL due predicate is re-applied at the claim (not only at
        SELECT); an expired lease (``<= now``) is reclaimable while a valid one is exclusive (R17).
        The caller commits.

        ``force_due`` (DATA-001 P2, Yua item 4) drops ONLY the ``next_attempt_epoch`` backoff predicate,
        for the explicit synchronous :meth:`drive_intent` path: an inline re-drive after a 'retry' must
        re-apply immediately rather than wait out the backoff a background worker paces on. It NEVER
        relaxes the lease-exclusivity guard, so a valid owner (the worker) still cannot be stolen."""
        expiry = now + self._lease_ttl
        due = "" if force_due else "AND (next_attempt_epoch IS NULL OR next_attempt_epoch <= ?) "
        params = (token, expiry, opk, now) if force_due else (token, expiry, opk, now, now)
        cur = con.execute(
            "UPDATE lifecycle_outbox SET lease_owner=?, lease_expires_epoch=? "
            "WHERE operation_key=? AND state IN ('PENDING','APPLIED') "
            f"{due}"
            "AND (lease_owner IS NULL OR lease_expires_epoch <= ?)",
            params,
        )
        return bool(cur.rowcount == 1)

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
        """Increment ``attempts`` and (re)schedule ``next_attempt_epoch`` — plus an optional
        ``failure_class`` / terminal ``state`` (stamping ``terminal_epoch``) — in ONE owner-guarded
        transaction (R15/R16), releasing the lease atomically when ``release``. The caller invokes
        this ONLY on a claimed ACTUAL apply outcome, so ``attempts`` always advances by one; a
        success/ABANDON passes ``reschedule=False`` (clears the schedule) and PRESERVES ``attempts``.
        A durably classified ``unknown`` is rescheduled forever, never abandoned by count."""
        now = self._now()
        guard, gparams = self._owner_guard(owner)
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            row = con.execute(
                "SELECT attempts FROM lifecycle_outbox WHERE operation_key=?", (opk,)
            ).fetchone()
            attempts = ((row[0] or 0) if row else 0) + 1
            next_epoch = (now + self._backoff(attempts)) if reschedule else None
            schedule_cols = "next_attempt_epoch=?"
            schedule_vals: list[object] = [next_epoch]
            if failure_class is not None:
                schedule_cols += ", failure_class=?"
                schedule_vals.append(failure_class)
            if state is not None:
                schedule_cols += ", state=?"
                schedule_vals.append(state)
                if state in ("FINAL", "ABANDONED"):
                    schedule_cols += ", terminal_epoch=?"
                    schedule_vals.append(now)
            if release:
                schedule_cols += ", lease_owner=NULL, lease_expires_epoch=NULL"
            # ONE transaction so a fault mid-persist (R15) rolls BOTH the attempts increment AND the
            # schedule/class/disposition write back — attempts is NEVER advanced with a stale/missing
            # next_attempt_epoch. The seam between them is inside the txn.
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    f"UPDATE lifecycle_outbox SET attempts=? WHERE operation_key=?{guard}",
                    (attempts, opk, *gparams),
                )
                self._checkpoint("after_attempts_before_schedule")
                con.execute(
                    f"UPDATE lifecycle_outbox SET {schedule_cols} WHERE operation_key=?{guard}",
                    (*schedule_vals, opk, *gparams),
                )
                con.execute("COMMIT")
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()

    def _mark(
        self, opk: str, state: str, *, owner: str | None = None, release: bool = False
    ) -> None:
        """Set a row's state (stamping ``terminal_epoch`` on FINAL/ABANDONED), owner-guarded, and —
        when ``release`` — clearing the lease atomically in the SAME write (S4 R16)."""
        guard, gparams = self._owner_guard(owner)
        sets = "state=?"
        vals: list[object] = [state]
        if state in ("FINAL", "ABANDONED"):
            sets += ", terminal_epoch=?"
            vals.append(self._now())
        if release:
            sets += ", lease_owner=NULL, lease_expires_epoch=NULL"
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute(
                f"UPDATE lifecycle_outbox SET {sets} WHERE operation_key=?{guard}",
                (*vals, opk, *gparams),
            )
            con.commit()
        finally:
            con.close()

    def _has_event_payload(self, opk: str) -> bool:
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            row = con.execute(
                "SELECT event_payload FROM lifecycle_outbox WHERE operation_key=?", (opk,)
            ).fetchone()
        finally:
            con.close()
        return bool(row and row[0])

    # -- C4/ART-001 additive intent-kind extension ----------------------------------------- #

    def register_intent_handler(
        self, kind: str, handler: Callable[[CustomIntentContext], str]
    ) -> None:
        """Register the APPLY handler for a non-transition intent ``kind`` (e.g. ``'artifact_index'``).
        The reconcile loop dispatches a claimed row of this kind to the handler and maps its outcome
        (``'confirmed'``/``'fence'``/``'retry'``) onto the standard terminal/retry disposition; the
        coordinator still owns admission, claim/lease, attempts/backoff, and the terminal write."""
        if not kind or kind == "lifecycle_transition":
            raise ValueError("intent kind must be non-empty and not 'lifecycle_transition'")
        self._intent_handlers[kind] = handler

    #: Max serialized custom-intent patch accepted at admission (DATA-001 P2). The payload is the
    #: canonical content + narrow fields + a recompute fingerprint — never a raw vector blob — so this
    #: bounds the durable outbox row. A larger payload fails TRUTHFULLY at admission.
    _MAX_PATCH_JSON_BYTES = 64 * 1024

    def enqueue_custom_intent(
        self,
        *,
        kind: str,
        object_id: str,
        namespace: str,
        collection: str,
        patch_json: str | None = None,
        operation_key: str | None = None,
    ) -> str:
        """Durably admit ONE custom (non-transition) intent of ``kind`` — the generalization of
        :meth:`enqueue_index_intent`. Same cap gate + ``ux_active_intent`` idempotency (one active
        intent per ``(collection, object_id)``); backpressure NEVER raises. ``patch_json`` is the
        durable intent payload the handler replays from with no caller memory (DATA-001 P2); it is
        validated as JSON and size-bounded HERE, so a malformed/oversized payload fails truthfully at
        admission rather than silently mid-apply. ``operation_key`` may be supplied by a caller that
        must drive its own just-admitted intent inline via :meth:`drive_intent` (else one is generated).
        Returns ``'admitted'`` / ``'already_active'`` / ``'at_capacity'`` (on capacity the caller MUST
        record a visible terminal disposition)."""
        if not kind or kind == "lifecycle_transition":
            raise ValueError(
                f"custom intent kind must be non-empty and non-transition; got {kind!r}"
            )
        if patch_json is not None:
            if len(patch_json.encode("utf-8")) > self._MAX_PATCH_JSON_BYTES:
                raise ValueError(
                    f"patch_json exceeds {self._MAX_PATCH_JSON_BYTES} bytes; persist a recompute "
                    "fingerprint + narrow fields, never a raw vector blob"
                )
            try:
                json.loads(patch_json)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(f"patch_json is not valid JSON: {exc}") from exc
        opk = operation_key or f"{kind}:{object_id}:{self._new_token()}"
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                if self._over_cap(con):
                    con.execute("ROLLBACK")
                    return "at_capacity"
                con.execute(
                    "INSERT INTO lifecycle_outbox "
                    "(operation_key,object_id,collection,namespace,state,intent_kind,patch_json,"
                    "attempts) VALUES (?,?,?,?,'PENDING',?,?,0)",
                    (opk, object_id, collection, namespace, kind, patch_json),
                )
                con.execute("COMMIT")
                return "admitted"
            except sqlite3.IntegrityError:
                # ux_active_intent: an intent is already active for this (collection, object_id).
                con.execute("ROLLBACK")
                return "already_active"
        finally:
            con.close()

    def enqueue_index_intent(
        self, *, object_id: str, namespace: str, collection: str = "musubi_artifact"
    ) -> str:
        """Backward-compatible artifact_index admission (C4/ART-001) — a thin wrapper over
        :meth:`enqueue_custom_intent` with ``kind='artifact_index'`` and no patch payload (the indexer
        re-derives from Qdrant + the on-disk blob). Behavior and return contract are unchanged."""
        return self.enqueue_custom_intent(
            kind="artifact_index", object_id=object_id, namespace=namespace, collection=collection
        )

    def reconcile_once(self, *, limit: int = 100) -> ReconcileReport:
        """Run one reconcile pass while holding the S6 shared maintenance barrier."""
        with self._barrier_admit(role="reconcile") as admitted:
            if admitted is not True:
                return ReconcileReport()
            return self._reconcile_locked(limit=limit)

    def drive_intent(self, operation_key: str) -> ReconcileReport:
        """Claim and drive EXACTLY ONE just-admitted operation to a terminal/pending outcome, using the
        SAME lease + state-machine + custom-handler path as the reconcile worker — but scoped to this
        ``operation_key`` ALONE. It NEVER selects, claims, or drives any other queued intent.

        This is the synchronous-inline seam (DATA-001 P2): a caller admits a durable intent, then drives
        only its own operation to completion so it keeps a committed-return contract, while the durable
        row remains the crash net for the worker. If the row is already terminal/unknown, or a valid
        owner (the worker) already holds it, this is a bounded no-op — the caller then reads back and
        returns a truthful pending/typed result, never an uncommitted object."""
        with self._barrier_admit(role="reconcile") as admitted:
            if admitted is not True:
                return ReconcileReport()
            return self._drive_operation_locked(operation_key)

    def _drive_operation_locked(self, operation_key: str) -> ReconcileReport:
        now = self._now()
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            row = con.execute(
                "SELECT operation_key,object_id,collection,target_state,expected_version,namespace,"
                "actor,reason,event_id,state,patch_json,intent_kind FROM lifecycle_outbox "
                "WHERE operation_key = ? AND state IN ('PENDING','APPLIED')",
                (operation_key,),
            ).fetchone()
        finally:
            con.close()
        counts = {"claimed": 0, "finalized": 0, "pending": 0, "abandoned": 0}
        if row is None:
            return ReconcileReport(**counts)  # already terminal / unknown — nothing to drive.
        (opk, oid, coll, tstate, ver, ns, actor, reason, event_id, state, patch_json, ikind) = row
        token = self._new_token()
        cc = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None)
        try:
            # explicit inline drive == drive NOW: bypass the retry backoff (never the lease guard).
            got = self._claim(cc, opk, now, token, force_due=True)
            cc.commit()
        finally:
            cc.close()
        if not got:
            return ReconcileReport(**counts)  # a valid owner (the worker) holds it — leave it be.
        counts["claimed"] += 1
        if ikind and ikind != "lifecycle_transition":
            self._drive_custom_intent(ikind, opk, oid, coll, ns, token, counts, patch_json)
        else:
            self._reconcile_claimed(
                opk,
                oid,
                coll,
                tstate,
                ver,
                ns,
                actor,
                reason,
                event_id,
                state,
                patch_json,
                token,
                counts,
            )
        return ReconcileReport(**counts)

    def _reconcile_locked(self, *, limit: int = 100) -> ReconcileReport:
        """One reconcile pass (S4). Select DUE non-terminal rows (fair, oldest-first: never-scheduled
        first, then earliest ``next_attempt_epoch``, then insertion order), atomically CLAIM each,
        then drive it toward a terminal outcome:

        - APPLIED (crash before FINAL) → finalize (readback-only, NO attempt increment).
        - PENDING whose Qdrant target/version is already visible (crash after apply, before APPLIED)
          → readback-confirm then mark APPLIED + finalize, WITHOUT a second effective apply.
        - otherwise an ACTUAL apply (the ONLY attempts-incrementing site; the event is rebuilt from
          the stored namespace/actor/reason + preserved event_id if the row is pre-persist):
          confirmed → APPLIED → FINAL; fence → ABANDONED (terminal); corrupt → keep PENDING +
          reschedule (transient-like, never abandoned); an exception classified terminal → ABANDONED,
          transient/unknown → keep PENDING + increment + bounded backoff (an unknown is never
          abandoned by count). The lease is held through APPLIED and released atomically at the
          terminal/pending disposition; every post-claim write is owner-guarded."""
        self._checkpoint("reconcile_entered")
        now = self._now()
        con = store.connect(self._db, busy_timeout_ms=self._busy_timeout_ms)
        try:
            rows = con.execute(
                "SELECT operation_key,object_id,collection,target_state,expected_version,namespace,"
                "actor,reason,event_id,state,patch_json,intent_kind FROM lifecycle_outbox "
                "WHERE state IN ('PENDING','APPLIED') "
                "AND (next_attempt_epoch IS NULL OR next_attempt_epoch <= ?) "
                "ORDER BY (next_attempt_epoch IS NOT NULL), next_attempt_epoch, rowid LIMIT ?",
                (now, limit),
            ).fetchall()
        finally:
            con.close()
        counts = {"claimed": 0, "finalized": 0, "pending": 0, "abandoned": 0}
        for (
            opk,
            oid,
            coll,
            tstate,
            ver,
            ns,
            actor,
            reason,
            event_id,
            state,
            patch_json,
            ikind,
        ) in rows:
            token = self._new_token()
            self._checkpoint(
                "before_claim"
            )  # two-process claim-race barrier (both reach the claim)
            cc = store.connect(
                self._db, busy_timeout_ms=self._busy_timeout_ms, isolation_level=None
            )
            try:
                got = self._claim(cc, opk, now, token)
                cc.commit()
            finally:
                cc.close()
            if not got:
                continue  # not due-and-claimable now, or lost the claim race (a valid owner holds it)
            counts["claimed"] += 1
            self._checkpoint("after_claim_before_qdrant")  # durable-claim barrier / crash point
            # C4/ART-001: a non-transition intent-kind delegates its APPLY to a registered handler;
            # the built-in lifecycle-transition path (ikind NULL/'lifecycle_transition') is untouched.
            if ikind and ikind != "lifecycle_transition":
                self._drive_custom_intent(ikind, opk, oid, coll, ns, token, counts, patch_json)
                continue
            self._reconcile_claimed(
                opk,
                oid,
                coll,
                tstate,
                ver,
                ns,
                actor,
                reason,
                event_id,
                state,
                patch_json,
                token,
                counts,
            )
        # R19 (S5): emit the UNLABELED pending-depth gauge exactly ONCE per reconcile pass.
        self._observe_pending()
        return ReconcileReport(**counts)

    def _reconcile_claimed(
        self,
        opk: str,
        oid: str,
        coll: str,
        tstate: str,
        ver: int,
        ns: str,
        actor: str,
        reason: str,
        event_id: str,
        state: str,
        patch_json: str,
        token: str,
        counts: dict[str, int],
    ) -> None:
        if state == "APPLIED":
            # crash after APPLIED, before FINAL: finalize (readback-only, no attempt increment).
            self._finalize(opk, owner=token)
            counts["finalized"] += 1
            return
        # PENDING: readback FIRST — a crash after the Qdrant apply, before APPLIED, is recognized and
        # finalized WITHOUT a second effective apply (Yua S4: already-visible target/version).
        if self._cur(coll, oid, ns) == (ver + 1, tstate):
            self._mark_applied(opk, oid, tstate, owner=token)
            self._finalize(opk, owner=token)
            counts["finalized"] += 1
            return
        # ACTUAL apply. Reconstruct the intent from stored admission truth (self-sufficient) and
        # rebuild the persisted event with the PRESERVED event_id if the row is pre-persist.
        stored_patch = json.loads(patch_json)
        intent = TransitionIntent(
            collection=coll,
            object_id=oid,
            namespace=ns,
            expected_version=ver,
            target_state=tstate,
            actor=actor,
            reason=reason,
            operation_key=opk,
            updated_at=str(stored_patch["updated_at"]),
            updated_epoch=float(stored_patch["updated_epoch"]),
            superseded_by=stored_patch.get("superseded_by"),
            supersedes=tuple(stored_patch.get("supersedes", ())),
            merged_from=tuple(stored_patch.get("merged_from", ())),
            contradicts=tuple(stored_patch.get("contradicts", ())),
            promoted_to=stored_patch.get("promoted_to"),
            promoted_at=stored_patch.get("promoted_at"),
        )
        if not self._has_event_payload(opk):
            try:
                self._persist_event(intent, opk, event_id)
            except _TerminalValidation:
                self._persist_attempt(
                    opk,
                    reschedule=False,
                    state="ABANDONED",
                    failure_class="terminal",
                    owner=token,
                    release=True,
                )
                counts["abandoned"] += 1
                return
        try:
            status = self._apply_conditional(opk, coll, oid, _intended_patch(intent))
        except Exception as exc:  # classified terminal vs retryable
            cls = self._classify(exc)
            # R19 (S5): PII-free failure observability on the durable classification (unknown→transient
            # at the external metric label; the log carries the durable class only).
            self._observe_failure(cls)
            if cls == "terminal":
                self._persist_attempt(
                    opk,
                    reschedule=False,
                    state="ABANDONED",
                    failure_class="terminal",
                    owner=token,
                    release=True,
                )
                counts["abandoned"] += 1
            else:
                self._persist_attempt(
                    opk, reschedule=True, failure_class=cls, owner=token, release=True
                )
                counts["pending"] += 1
            return
        if status == "fence":
            self._persist_attempt(
                opk,
                reschedule=False,
                state="ABANDONED",
                failure_class="terminal",
                owner=token,
                release=True,
            )
            counts["abandoned"] += 1
            return
        if status == "corrupt":
            self._persist_attempt(
                opk, reschedule=True, failure_class="transient", owner=token, release=True
            )
            counts["pending"] += 1
            return
        # confirmed: the attempt happened (increment, no reschedule); hold the lease through APPLIED,
        # release it in the FINAL move.
        self._persist_attempt(opk, reschedule=False, owner=token)
        # R17 crash seam: Qdrant is mutated and the row is still PENDING (+leased); a crash HERE, before
        # the APPLIED commit, leaves a durable side effect that a later reclaim readback-confirms and
        # finalizes WITHOUT a second apply (the readback branch above). Default no-op; no production exit.
        self._checkpoint("after_qdrant_before_applied")
        self._mark_applied(opk, oid, tstate, owner=token)
        self._finalize(opk, owner=token)
        counts["finalized"] += 1


__all__ = [
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_BACKOFF_MAX",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_PENDING_CAP",
    "LifecycleTransitionCoordinator",
    "ReconcileReport",
    "TransitionError",
    "TransitionFinal",
    "TransitionIntent",
    "TransitionOutcome",
    "TransitionPending",
]
