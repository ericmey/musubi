"""Canonical ``transition()`` — the only code path that mutates ``state``.

Behaviour (from [[04-data-model/lifecycle#Transition function]]):

1. Fetch the current object. We scan each plane collection until we find the
   point; this is intentionally simple — the lifecycle worker deals in
   thousands, not millions, of objects per tick.
2. Validate ``(current_state, target_state)`` against the legal-transition
   table in :mod:`musubi.types.lifecycle_event`.
3. Apply the transition: update ``state``, bump ``updated_at`` /
   ``updated_epoch``, increment ``version``.
4. Apply lineage updates (supersession, merge-in, etc.) and reject cycles.
5. Construct a :class:`LifecycleEvent`, hand it to the
   :class:`LifecycleEventSink` for durable persistence.
6. Return ``Ok(TransitionResult)`` or ``Err(TransitionError)``.

Invalid transitions never mutate the Qdrant payload — the error is surfaced
before any ``set_payload`` call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionFinal,
    TransitionIntent,
    TransitionPending,
)
from musubi.lifecycle.events import LifecycleEventSink
from musubi.store.names import COLLECTION_NAMES
from musubi.types.common import (
    KSUID,
    Err,
    LifecycleState,
    Ok,
    Result,
    epoch_of,
    utc_now,
)
from musubi.types.lifecycle_event import (
    LifecycleEvent,
    ObjectType,
    is_legal_transition,
    legal_next_states,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class LineageUpdates(BaseModel):
    """Optional lineage-field changes to apply during a transition.

    Only the fields provided here are mutated. None of these are validated
    against the target object's schema at construction time — the transition
    function re-validates through the pydantic model of the target type.
    """

    model_config = ConfigDict(extra="forbid")

    superseded_by: KSUID | None = None
    supersedes: list[KSUID] = Field(default_factory=list)
    merged_from: list[KSUID] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)
    promoted_to: KSUID | None = None
    promoted_at: datetime | None = None

    def to_payload_patch(self) -> dict[str, Any]:
        """Serialise as a dict suitable for ``set_payload``; empty keys omitted."""
        patch: dict[str, Any] = {}
        if self.superseded_by is not None:
            patch["superseded_by"] = self.superseded_by
        if self.supersedes:
            patch["supersedes"] = list(self.supersedes)
        if self.merged_from:
            patch["merged_from"] = list(self.merged_from)
        if self.contradicts:
            patch["contradicts"] = list(self.contradicts)
        if self.promoted_to is not None:
            patch["promoted_to"] = self.promoted_to
        if self.promoted_at is not None:
            patch["promoted_at"] = self.promoted_at.isoformat()
        return patch

    def to_event_changes(self) -> dict[str, Any]:
        """Serialise as the ``lineage_changes`` field on :class:`LifecycleEvent`."""
        return self.to_payload_patch()


@dataclass(frozen=True)
class TransitionResult:
    """Outcome of a successful :func:`transition`."""

    object_id: KSUID
    object_type: ObjectType
    from_state: LifecycleState
    to_state: LifecycleState
    version: int
    event: LifecycleEvent


@dataclass(frozen=True)
class TransitionError:
    """Typed error from a failed :func:`transition` call.

    ``code`` is one of:

    - ``not_found``            — no object with that id across any plane.
    - ``illegal_transition``   — ``(from_state, to_state)`` not in the table.
    - ``missing_reason``       — the ``reason`` argument was empty.
    - ``circular_supersession`` — supersession would create A → B → A.
    - ``invariant_violation``  — model validation failed on the updated payload.
    - ``lifecycle_event_write_failed`` — mutation committed, audit persistence refused.
    - ``version_fence_violation``     — expected_version did not match current_version.
    """

    code: str
    message: str
    from_state: LifecycleState | None = None
    to_state: LifecycleState | None = None
    allowed: tuple[LifecycleState, ...] = field(default_factory=tuple)


# Mapping from Qdrant collection name to the canonical ObjectType string.
# Kept here (not in names.py) because the collection → object_type coupling
# is a lifecycle concern, not a storage-layout concern.
_COLLECTION_TO_OBJECT_TYPE: dict[str, ObjectType] = {
    "musubi_episodic": "episodic",
    "musubi_curated": "curated",
    "musubi_concept": "concept",
    "musubi_artifact": "artifact",
    "musubi_thought": "thought",
}


def transition(
    client: QdrantClient,
    *,
    coordinator: LifecycleTransitionCoordinator,
    object_id: KSUID,
    target_state: LifecycleState,
    actor: str,
    reason: str,
    lineage_updates: LineageUpdates | None = None,
    correlation_id: str = "",
    sink: LifecycleEventSink | None = None,
    expected_version: int | None = None,
) -> Result[TransitionResult | TransitionPending, TransitionError]:
    """Apply a state change to ``object_id``, recording an audit event.

    Lookup, legal-transition checks, lineage-cycle validation, and deterministic
    intent construction remain here. The required injected ``coordinator`` owns
    durable admission, version-fenced mutation, readback, event persistence, and
    reconciliation. ``sink`` remains as a source-compatible argument for callers
    being migrated in H5; it is deliberately not a second persistence path.
    """
    if not reason:
        return Err(
            error=TransitionError(
                code="missing_reason",
                message="`reason` argument must be a non-empty string",
                to_state=target_state,
            )
        )

    located = _locate_object(client, object_id=object_id)
    if located is None:
        return Err(
            error=TransitionError(
                code="not_found",
                message=f"no object with object_id={object_id!r} in any plane",
                to_state=target_state,
            )
        )
    collection, payload = located
    object_type = _COLLECTION_TO_OBJECT_TYPE[collection]
    current_state: LifecycleState = payload.get("state", "provisional")
    current_version = int(payload.get("version", 1))

    # Concurrent-modification check runs BEFORE the legality check so the
    # "last writer wins with logged warning" contract in spec bullet 13
    # produces its warning even when the race collapses onto an illegal
    # transition from the actual current state.
    if expected_version is not None and expected_version != current_version:
        return Err(
            error=TransitionError(
                code="version_fence_violation",
                message=(
                    f"concurrent transition on {object_id}: expected_version={expected_version}, "
                    f"current_version={current_version}"
                ),
                from_state=current_state,
                to_state=target_state,
            )
        )

    if not is_legal_transition(object_type, current_state, target_state):
        allowed = tuple(sorted(legal_next_states(object_type, current_state)))
        return Err(
            error=TransitionError(
                code="illegal_transition",
                message=(
                    f"{object_type}: {current_state} → {target_state} not permitted; "
                    f"allowed from {current_state}: {list(allowed)}"
                ),
                from_state=current_state,
                to_state=target_state,
                allowed=allowed,
            )
        )

    lineage_patch = lineage_updates.to_payload_patch() if lineage_updates else {}
    if _would_cause_supersession_cycle(
        client,
        collection=collection,
        object_id=object_id,
        new_superseded_by=lineage_patch.get("superseded_by"),
    ):
        return Err(
            error=TransitionError(
                code="circular_supersession",
                message=(
                    f"transition rejected: object_id={object_id!r} -> "
                    f"superseded_by={lineage_patch.get('superseded_by')!r} "
                    f"would form a cycle"
                ),
                from_state=current_state,
                to_state=target_state,
            )
        )

    del sink
    now = utc_now()
    lineage = lineage_updates or LineageUpdates()
    intent = TransitionIntent(
        collection=collection,
        object_id=object_id,
        namespace=str(payload["namespace"]),
        expected_version=current_version if expected_version is None else expected_version,
        target_state=target_state,
        actor=actor,
        reason=reason,
        updated_at=now.isoformat(),
        updated_epoch=epoch_of(now),
        superseded_by=lineage.superseded_by,
        supersedes=tuple(lineage.supersedes),
        merged_from=tuple(lineage.merged_from),
        contradicts=tuple(lineage.contradicts),
        promoted_to=lineage.promoted_to,
        promoted_at=lineage.promoted_at.isoformat() if lineage.promoted_at is not None else None,
    )
    outcome = coordinator.transition(intent)
    if isinstance(outcome, Err):
        return Err(
            error=TransitionError(
                code=outcome.error.code,
                message=f"lifecycle transition failed: {outcome.error.code}",
                from_state=current_state,
                to_state=target_state,
            )
        )
    if isinstance(outcome.value, TransitionPending):
        return Ok(value=outcome.value)
    assert isinstance(outcome.value, TransitionFinal)
    event = LifecycleEvent(
        event_id=outcome.value.event_id,
        object_id=object_id,
        object_type=object_type,
        namespace=str(payload["namespace"]),
        from_state=current_state,
        to_state=target_state,
        actor=actor,
        reason=reason,
        lineage_changes=lineage.to_event_changes(),
        correlation_id=correlation_id,
    )
    return Ok(
        value=TransitionResult(
            object_id=object_id,
            object_type=object_type,
            from_state=current_state,
            to_state=target_state,
            version=current_version + 1,
            event=event,
        )
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _locate_object(client: QdrantClient, *, object_id: KSUID) -> tuple[str, dict[str, Any]] | None:
    """Scan each plane collection for ``object_id``. Returns ``(collection, payload)``."""
    for collection in _COLLECTION_TO_OBJECT_TYPE:
        records = _scroll_by_object_id(client, collection=collection, object_id=object_id)
        if records:
            return collection, records[0]
    return None


def _scroll_by_object_id(
    client: QdrantClient, *, collection: str, object_id: KSUID
) -> list[dict[str, Any]]:
    """Return the payload dict(s) for ``object_id`` in ``collection``, if any."""
    try:
        records, _ = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))
                ]
            ),
            limit=1,
            with_payload=True,
        )
    except Exception:
        # Collection may not exist in this client instance — treat as miss.
        return []
    payloads: list[dict[str, Any]] = []
    for rec in records:
        if rec.payload:
            payloads.append(dict(rec.payload))
    return payloads


def _lookup_point_id(client: QdrantClient, *, collection: str, object_id: KSUID) -> str | int:
    """Find the Qdrant point id for ``object_id``. Raises if missing."""
    records, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=1,
        with_payload=False,
    )
    if not records:
        raise LookupError(f"point for object_id={object_id!r} missing in {collection!r}")
    pid = records[0].id
    # Qdrant accepts both int and str (UUID) point ids; pass through.
    if isinstance(pid, (int, str)):
        return pid
    raise TypeError(f"unexpected point id type: {type(pid)!r}")


def _would_cause_supersession_cycle(
    client: QdrantClient,
    *,
    collection: str,
    object_id: KSUID,
    new_superseded_by: KSUID | None,
) -> bool:
    """Return ``True`` iff setting ``object_id.superseded_by = new`` would cycle.

    Walks the supersession chain starting at ``new_superseded_by`` and fails
    if it ever reaches ``object_id``. Bounded to 64 hops to avoid pathological
    data on disk stalling a transition.
    """
    if new_superseded_by is None:
        return False
    if new_superseded_by == object_id:
        return True
    seen: set[str] = set()
    cursor: str | None = new_superseded_by
    for _ in range(64):
        if cursor is None:
            return False
        if cursor == object_id:
            return True
        if cursor in seen:
            return False  # pre-existing unrelated loop — not our problem
        seen.add(cursor)
        records = _scroll_by_object_id(client, collection=collection, object_id=cursor)
        if not records:
            return False
        cursor = records[0].get("superseded_by")
    return False


# Suppress unused-import hint — ``COLLECTION_NAMES`` is exported via
# ``_COLLECTION_TO_OBJECT_TYPE`` key ordering; keep the import visible so a
# future maintainer knows where the canonical list lives.
assert set(_COLLECTION_TO_OBJECT_TYPE) <= set(COLLECTION_NAMES)


__all__ = [
    "LineageUpdates",
    "TransitionError",
    "TransitionResult",
    "transition",
]
