"""``EpisodicMemory`` — what happened.

Primary write target for adapters (MCP, LiveKit, etc.). States allowed:
``provisional``, ``matured``, ``demoted``, ``archived``, ``superseded``.
See [[04-data-model/lifecycle#EpisodicMemory]] for the transition diagram.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from musubi.types.base import MemoryObject
from musubi.types.common import LifecycleState, Modality, ensure_utc, utc_now

_EPISODIC_STATES: frozenset[LifecycleState] = frozenset(
    {"provisional", "matured", "demoted", "archived", "superseded"}
)


class EpisodicMemory(MemoryObject):
    """One captured event — a message, a tool call, a system signal.

    ``event_at`` is when the thing happened in the world. ``ingested_at`` is when
    Musubi learned about it (typically == ``created_at``).
    """

    state: Literal["provisional", "matured", "demoted", "archived", "superseded"] = "provisional"
    event_at: datetime = Field(default_factory=utc_now)
    ingested_at: datetime = Field(default_factory=utc_now)
    modality: Modality = "text"
    participants: list[str] = Field(default_factory=list)
    source_context: str = Field(
        default="",
        description="Freeform origin hint, e.g. 'Claude Code session 2026-04-17 14:23'.",
    )
    topics: list[str] = Field(default_factory=list)
    importance_last_scored_at: datetime | None = None

    @property
    def importance_last_scored_epoch(self) -> float | None:
        from musubi.types.common import epoch_of

        return epoch_of(self.importance_last_scored_at) if self.importance_last_scored_at else None

    @model_validator(mode="after")
    def _normalise_times(self) -> EpisodicMemory:
        object.__setattr__(self, "event_at", ensure_utc(self.event_at))
        object.__setattr__(self, "ingested_at", ensure_utc(self.ingested_at))
        if self.importance_last_scored_at is not None:
            object.__setattr__(
                self, "importance_last_scored_at", ensure_utc(self.importance_last_scored_at)
            )
        return self


__all__ = ["EpisodicMemory"]
