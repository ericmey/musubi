"""LifecycleTransitionCoordinator — C6b Phase-1 durable-intent outbox (S2: admission only).

S2 implements the ADMISSION half of the transactional outbox. A transition is
admitted by writing a durable ``PENDING`` row to ``lifecycle_outbox`` inside ONE
``BEGIN IMMEDIATE`` write transaction that:

- enforces a global non-terminal pending **cap** (count of ``PENDING``/``APPLIED``
  rows ``>= pending_cap`` → reject, write no row), and
- enforces **one active intent per ``(collection, object_id)``** via the partial
  unique index ``ux_active_intent`` (a second concurrent begin raises
  ``IntegrityError``).

A successful admission returns ``Ok(TransitionPending)``; the durable row is
committed BEFORE any Qdrant mutation would occur, so a bounded ``Err`` on
cap / single-active / durable-begin failure guarantees Qdrant is untouched.

S2 STOPS at ``PENDING``. Conditional apply + full-readback finalize
(``Ok(TransitionFinal)``), reconciliation/leases, ``operation_key`` idempotent
replay, and the maintenance/rollback barrier are LATER slices (S3/S4/S6). This
module therefore never mutates Qdrant and never finalizes.

Connection + schema come from the shared lifecycle store (WAL + busy_timeout);
admission uses an explicit ``BEGIN IMMEDIATE`` so concurrent admissions serialize
on the write lock. A private ``_checkpoint(name)`` seam (default no-op) lets tests
inject a deterministic fault/crash at a named boundary; it is not a public switch
and performs no production ``os._exit``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from musubi.lifecycle import store
from musubi.types.common import Err, Ok, generate_ksuid

#: Default global cap on non-terminal (PENDING/APPLIED) outbox rows. A positive int;
#: there is no unbounded/None option (admission must always have a bound).
DEFAULT_PENDING_CAP = 10_000

#: Default lease TTL (seconds) — consumed by the S4 reconciler; validated here so an
#: invalid value fails at construction, but unused in the S2 admission-only path.
DEFAULT_LEASE_TTL = 30.0

#: Deterministic default patch timestamps so an intent constructed without explicit
#: ``updated_at`` yields a reproducible minimal patch (production callers pass real
#: values). Matches the accepted contract's fixed patch epoch.
_FIXED_UPDATED_AT = "2026-07-13T00:00:00+00:00"
_FIXED_UPDATED_EPOCH = datetime.fromisoformat(_FIXED_UPDATED_AT).timestamp()


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


@dataclass(frozen=True)
class TransitionPending:
    """Admitted, durably recorded, not yet applied. The public success outcome of S2."""

    operation_key: str
    event_id: str
    kind: str = "pending"


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


class _CapExceeded(Exception):
    """Internal sentinel: the atomic admission found the non-terminal backlog at/over
    the cap. ``transition()`` maps it to ``Err(code='cap_exceeded')``; no row is
    written and Qdrant is untouched."""


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
    return patch


def _canonical_patch_sha(patch: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(patch, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class LifecycleTransitionCoordinator:
    """Durable-intent admission coordinator (S2). One instance per process owns the
    connection policy and admission logic against the shared lifecycle SQLite DB."""

    def __init__(
        self,
        *,
        client: Any = None,
        db_path: Path,
        pending_cap: int = DEFAULT_PENDING_CAP,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> None:
        self._pending_cap = _validate_pending_cap(pending_cap)
        self._lease_ttl = _validate_lease_ttl(lease_ttl)
        self._client = client
        self._db = Path(db_path)
        #: Private fault-injection seam (default no-op); tests set it to raise/crash at
        #: a named boundary. Not a public switch.
        self._checkpoint: Callable[[str], None] = lambda _name: None
        conn = store.connect(self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS)
        try:
            store.ensure_schema(conn)
        finally:
            conn.close()

    def transition(self, intent: TransitionIntent) -> Ok[TransitionOutcome] | Err[TransitionError]:
        """Admit ``intent``: derive a stable operation key, write the durable PENDING
        outbox row atomically, and return ``Ok(TransitionPending)``. On admission
        failure return a bounded ``Err`` (cap / single-active / durable-begin) — no row
        is committed and Qdrant is never touched (admission precedes any apply)."""
        opk = self._key(intent)
        event_id = generate_ksuid()
        try:
            self._write_pending(intent, opk, event_id)
        except _CapExceeded:
            return Err(error=TransitionError(code="cap_exceeded"))
        except sqlite3.IntegrityError as exc:
            # Classify ONLY the ux_active_intent partial-unique (collection, object_id)
            # violation as active_intent_exists (SQLITE_CONSTRAINT_UNIQUE). A duplicate
            # operation_key (PRIMARYKEY) or any other constraint is a generic
            # durable-begin failure — do not conflate them with the single-active guard.
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE:
                return Err(error=TransitionError(code="active_intent_exists"))
            return Err(error=TransitionError(code="durable_begin_failed"))
        except (sqlite3.Error, store.LifecycleStoreError):
            # A real durable-begin failure: either a SQLite error (same class a genuine
            # disk error raises) OR a per-operation store.connect that could not establish
            # the WAL policy (LifecycleStoreError is a RuntimeError, NOT sqlite3.Error, so
            # it must be named explicitly). Bounded Err; no row, no mutation.
            return Err(error=TransitionError(code="durable_begin_failed"))
        # The PENDING row is durably COMMITTED here. The post-commit crash/race seam is
        # deliberately OUTSIDE the durable-begin catch: a fault at after_pending_commit
        # propagates (or os._exit crashes the process) — it must NEVER be mapped to
        # durable_begin_failed, because that would report a false failure on a row that is
        # already committed, driving a spurious retry.
        self._checkpoint("after_pending_commit")
        return Ok(value=TransitionPending(operation_key=opk, event_id=event_id))

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
            patch_sha,
            patch_json,
            self._intent_digest(intent),
            state,
            event_id,
        )
        insert = (
            "INSERT INTO lifecycle_outbox (operation_key,object_id,collection,target_state,"
            "expected_version,patch_sha,patch_json,intent_digest,state,event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)"
        )
        con = store.connect(
            self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS, isolation_level=None
        )
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
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


__all__ = [
    "DEFAULT_LEASE_TTL",
    "DEFAULT_PENDING_CAP",
    "LifecycleTransitionCoordinator",
    "TransitionError",
    "TransitionFinal",
    "TransitionIntent",
    "TransitionOutcome",
    "TransitionPending",
]
