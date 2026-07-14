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
failure are terminal (ABANDONED). Reconciliation/leases (S4) and the maintenance/rollback barrier
(S6) are later slices.

Connection + schema come from the shared lifecycle store (WAL + busy_timeout). A private
``_checkpoint(name)`` seam (default no-op) lets tests inject a deterministic fault/crash at a named
boundary; it is not a public switch and performs no production ``os._exit``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from qdrant_client import models

from musubi.lifecycle import store
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.lifecycle_event import LifecycleEvent

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
    """Durable-intent transition coordinator (S2 admission + S3 conditional apply/finalize). One
    instance per process owns the connection policy and transition logic against the shared
    lifecycle SQLite DB and the injected Qdrant client."""

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
        """Full lifecycle transition (S3): resolve operation_key idempotency, admit a durable
        PENDING row, persist a canonical lifecycle event BEFORE any mutation, conditionally apply
        the version-fenced mutation with a full readback, then atomically finalize.

        Returns ``Ok(TransitionFinal)`` on a confirmed apply + finalize; ``Ok(TransitionPending)``
        on a recoverable outcome (corrupt readback, or a transient/unknown apply failure) left for
        the S4 reconciler; or a bounded ``Err`` — ``cap_exceeded`` / ``active_intent_exists`` /
        ``durable_begin_failed`` / ``operation_key_conflict`` / ``version_fence_violation`` /
        ``terminal_apply_failure``."""
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
            status = self._apply_conditional(intent, opk)
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
        # (5) confirmed: the effective-apply marker + PENDING->APPLIED commit TOGETHER (correction
        # 3), so a crash can never leave an APPLIED row without its marker (or vice versa).
        self._mark_applied(intent, opk)
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
        con = store.connect(self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS)
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
        """Read the object's payload, requesting enough rows to PROVE exactly one match for
        ``object_id`` within ``namespace`` (returns ``(payload, count)``; empty payload if none)."""
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
                ]
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
                }
            )
        except (ValidationError, ValueError) as exc:
            # illegal transition / invalid field — a pre-mutation invariant failure -> terminal.
            raise _TerminalValidation(str(exc)) from exc
        payload_json = event.model_dump_json()
        con = store.connect(
            self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS, isolation_level=None
        )
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

    def _apply_conditional(self, intent: TransitionIntent, opk: str) -> str:
        """Send the EXACT patch fenced server-side (collection + object_id + namespace +
        expected_version), then FULL-readback and confirm (S3 correction 4). Returns
        ``'confirmed'`` | ``'fence'`` | ``'corrupt'``. The fenced ``set_payload`` matches zero
        points when the object is not at ``expected_version`` (a stale intent) -> the readback
        proves a fence."""
        patch = _intended_patch(intent)
        client = self._require_client()
        client.set_payload(
            collection_name=intent.collection,
            payload=dict(patch),
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=intent.object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=intent.namespace)
                    ),
                    models.FieldCondition(
                        key="version", match=models.MatchValue(value=intent.expected_version)
                    ),
                ]
            ),
        )
        actual, count = self._read_object(intent.collection, intent.object_id, intent.namespace)
        return self._confirm(patch, intent, actual, count)

    def _confirm(
        self, patch: dict[str, object], intent: TransitionIntent, actual: dict[str, Any], count: int
    ) -> str:
        """Confirm an apply from the ACTUAL readback (S3 correction 4). ``'fence'`` = stale /
        not-exactly-one / wrong identity-version-state (the fenced write matched zero points);
        ``'corrupt'`` = the version+state landed but an intended patch key is missing/mismatched
        (recoverable); ``'confirmed'`` = exactly one point, identity + namespace + version + state
        all correct, every intended key present, and the SHA over the ACTUAL-projected patch equals
        the intended SHA."""
        if count != 1:
            return "fence"
        if str(actual.get("namespace", "")) != intent.namespace:
            return "fence"
        object_id = actual.get("object_id")
        if object_id is not None and str(object_id) != intent.object_id:
            return "fence"
        if actual.get("version") != intent.expected_version + 1:
            return "fence"
        if actual.get("state") != intent.target_state:
            return "fence"
        for key in patch:
            if key not in actual:
                return "corrupt"
        projected = {key: actual[key] for key in patch}
        if _canonical_patch_sha(projected) != _canonical_patch_sha(patch):
            return "corrupt"
        return "confirmed"

    # -- S3: marker + APPLIED (one txn) and atomic finalize -------------------------- #

    def _mark_applied(self, intent: TransitionIntent, opk: str) -> None:
        """The confirmed effective-apply marker AND the PENDING->APPLIED move commit TOGETHER (S3
        correction 3). A marker key collision must verify identical object/target — never silently
        hide a mismatch."""
        con = store.connect(
            self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS, isolation_level=None
        )
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(
                    "INSERT INTO lifecycle_apply_markers "
                    "(operation_key, object_id, target_state) VALUES (?,?,?)",
                    (opk, intent.object_id, intent.target_state),
                )
            except sqlite3.IntegrityError:
                existing = con.execute(
                    "SELECT object_id, target_state FROM lifecycle_apply_markers "
                    "WHERE operation_key=?",
                    (opk,),
                ).fetchone()
                if (
                    existing is None
                    or existing[0] != intent.object_id
                    or existing[1] != intent.target_state
                ):
                    # a genuine marker mismatch — surfaced (the single outer handler rolls back).
                    raise
                # identical marker (idempotent re-apply) — fall through to the APPLIED move.
            cur = con.execute(
                "UPDATE lifecycle_outbox SET state='APPLIED' "
                "WHERE operation_key=? AND state='PENDING'",
                (opk,),
            )
            if cur.rowcount != 1:
                # the marker + APPLIED must move EXACTLY one PENDING row in this txn, else an apply
                # marker would orphan (committed with no APPLIED row) — refuse (S3 integrity hole 2).
                raise RuntimeError(
                    f"mark-applied matched {cur.rowcount} PENDING rows for {opk} (need exactly 1)"
                )
            con.execute("COMMIT")
        except Exception:
            # ONE rollback owner (S3 integrity hole 3): guard on in_transaction so a failure that
            # already ended the txn is not masked by a second "no transaction" rollback error.
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def _finalize(self, opk: str) -> None:
        """Atomic FINAL (S3 correction 1, R8 forward guard): insert the 8-column FINAL
        ``lifecycle_events`` row FROM the persisted event payload AND move APPLIED->FINAL (stamping
        ``terminal_epoch``) in ONE transaction, guarded on ``state='APPLIED'`` (exactly one row).
        A replayed ``event_id`` must be byte-identical to the stored payload — a conflicting
        collision is surfaced, never silently accepted."""
        con = store.connect(
            self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS, isolation_level=None
        )
        try:
            row = con.execute(
                "SELECT event_payload FROM lifecycle_outbox WHERE operation_key=?", (opk,)
            ).fetchone()
            if row is None or not row[0]:
                raise RuntimeError(f"finalize: no persisted event payload for {opk}")
            payload_json = str(row[0])
            event = LifecycleEvent.model_validate_json(payload_json)
            con.execute("BEGIN IMMEDIATE")
            try:
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
                        # a conflicting event collision — surfaced (the single outer handler rolls
                        # back), never silently accepted.
                        raise
                    # identical event (idempotent re-finalize) — proceed to the FINAL move.
                # Crash/fault seam INSIDE the finalize txn (R8): a fault here must roll back the
                # event insert too, leaving the row EXACTLY APPLIED (never a half-finalized FINAL
                # with no event, nor an orphaned event).
                self._checkpoint("inside_finalize_after_event_insert")
                cur = con.execute(
                    "UPDATE lifecycle_outbox SET state='FINAL', terminal_epoch=? "
                    "WHERE operation_key=? AND state='APPLIED'",
                    (self._now(), opk),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"finalize guard: expected exactly one APPLIED row for {opk}, "
                        f"updated {cur.rowcount}"
                    )
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
        correction 6)."""
        if isinstance(exc, _TerminalValidation):
            return True
        return bool(getattr(exc, "terminal", False))

    def _mark_terminal(self, opk: str) -> None:
        """Move a non-terminal row to ABANDONED, stamping ``terminal_epoch`` atomically."""
        con = store.connect(
            self._db, busy_timeout_ms=store.DEFAULT_BUSY_TIMEOUT_MS, isolation_level=None
        )
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
