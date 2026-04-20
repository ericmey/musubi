"""LifecycleEvent + allowed-transition tables.

Every state change on a ``MusubiObject`` emits exactly one ``LifecycleEvent``
(see [[04-data-model/lifecycle#No silent mutation]]). This module owns the
*shape* of the event and the *table* of what transitions are legal; the engine
that actually executes transitions lives in slice-lifecycle-engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from musubi.types.common import (
    KSUID,
    SCHEMA_VERSION,
    LifecycleState,
    Namespace,
    ensure_utc,
    epoch_of,
    generate_ksuid,
    utc_now,
)

ObjectType = str  # one of: "episodic" | "curated" | "concept" | "artifact" | "thought"


# Allowed (from_state → to_states) per object type. Sourced from
# [[04-data-model/lifecycle#Allowed transitions per type]].
_ALLOWED: dict[ObjectType, dict[LifecycleState, frozenset[LifecycleState]]] = {
    "episodic": {
        "provisional": frozenset({"matured", "archived"}),
        "matured": frozenset({"demoted", "superseded"}),
        "demoted": frozenset({"matured"}),  # operator reinstate
        "archived": frozenset({"matured"}),  # operator restore
        "superseded": frozenset(),
    },
    "curated": {
        "matured": frozenset({"superseded", "archived"}),
        "superseded": frozenset(),
        "archived": frozenset({"matured"}),  # operator restore
    },
    "concept": {
        "synthesized": frozenset({"matured"}),
        "matured": frozenset({"promoted", "demoted", "superseded"}),
        "promoted": frozenset(),  # terminal
        "demoted": frozenset({"matured"}),  # operator reinstate
        "superseded": frozenset(),
    },
    "artifact": {
        "matured": frozenset({"archived", "superseded"}),
        "archived": frozenset(),
        "superseded": frozenset(),
    },
    "thought": {
        "provisional": frozenset({"matured", "archived"}),
        "matured": frozenset({"archived"}),
        "archived": frozenset(),
    },
}


def allowed_states(object_type: ObjectType) -> frozenset[LifecycleState]:
    """Return the full set of states an object of this type may ever occupy."""
    try:
        return frozenset(_ALLOWED[object_type].keys())
    except KeyError as exc:
        raise ValueError(f"unknown object_type {object_type!r}") from exc


def is_legal_transition(
    object_type: ObjectType,
    from_state: LifecycleState,
    to_state: LifecycleState,
) -> bool:
    """``True`` iff ``from_state → to_state`` is permitted for ``object_type``."""
    try:
        return to_state in _ALLOWED[object_type][from_state]
    except KeyError:
        return False


def legal_next_states(
    object_type: ObjectType, from_state: LifecycleState
) -> frozenset[LifecycleState]:
    """Legal target states from ``from_state``; empty frozenset if terminal."""
    try:
        return _ALLOWED[object_type][from_state]
    except KeyError as exc:
        raise ValueError(f"no transitions defined from {object_type}/{from_state}") from exc


class LifecycleEvent(BaseModel):
    """One audit-log row describing a state change.

    Stored in sqlite (canonical) and optionally mirrored to a Qdrant audit
    collection for semantic search over the log (reflection / introspection).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    event_id: KSUID = Field(default_factory=generate_ksuid)
    object_id: KSUID
    object_type: ObjectType
    namespace: Namespace
    schema_version: int = SCHEMA_VERSION
    from_state: LifecycleState
    to_state: LifecycleState
    actor: str = Field(
        min_length=1,
        description="Presence or system identifier that triggered the transition.",
    )
    reason: str = Field(min_length=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    occurred_epoch: float | None = None
    lineage_changes: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(
        default="",
        description="Request-scoped correlation ID; empty on background-job events.",
    )

    @model_validator(mode="after")
    def _validate(self) -> LifecycleEvent:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        if self.occurred_epoch is None:
            object.__setattr__(self, "occurred_epoch", epoch_of(self.occurred_at))

        if not is_legal_transition(self.object_type, self.from_state, self.to_state):
            try:
                allowed = sorted(legal_next_states(self.object_type, self.from_state))
            except ValueError:
                allowed = []  # unknown object_type or from_state
            raise ValueError(
                f"illegal transition for {self.object_type}: "
                f"{self.from_state} -> {self.to_state} "
                f"(allowed from {self.from_state}: {allowed})"
            )
        return self


class CaptureEvent(BaseModel):
    """An audit-log row describing the initial capture/creation of an object."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    event_id: KSUID = Field(default_factory=generate_ksuid)
    object_id: KSUID
    object_type: ObjectType
    namespace: Namespace
    schema_version: int = SCHEMA_VERSION
    state: LifecycleState
    actor: str = Field(
        min_length=1,
        description="Presence or system identifier that triggered the capture.",
    )
    reason: str = Field(min_length=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    occurred_epoch: float | None = None
    lineage_changes: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str = Field(
        default="",
        description="Request-scoped correlation ID; empty on background-job events.",
    )

    @model_validator(mode="after")
    def _validate(self) -> CaptureEvent:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        if self.occurred_epoch is None:
            object.__setattr__(self, "occurred_epoch", epoch_of(self.occurred_at))
        return self


__all__ = [
    "CaptureEvent",
    "LifecycleEvent",
    "ObjectType",
    "allowed_states",
    "is_legal_transition",
    "legal_next_states",
]
