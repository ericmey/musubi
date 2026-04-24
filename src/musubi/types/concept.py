"""``SynthesizedConcept`` — what seems to be emerging.

Machine-generated hypotheses produced by the synthesis job. A concept is
provisional until it matures (24h without contradiction); from ``matured`` it
may promote to curated or demote to the cold store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from musubi.types.base import MemoryObject
from musubi.types.common import KSUID, LifecycleState, ensure_utc

_CONCEPT_STATES: frozenset[LifecycleState] = frozenset(
    {"synthesized", "matured", "promoted", "demoted", "superseded"}
)


class SynthesizedConcept(MemoryObject):
    """A hypothesis bridging episodic evidence and potential curated knowledge.

    ``merged_from`` (inherited from ``MemoryObject``) lists the EpisodicMemory ids
    the concept was synthesised from; ``synthesis_rationale`` is the LLM's
    one-to-three-sentence explanation of why those memories cluster.
    """

    state: Literal["synthesized", "matured", "promoted", "demoted", "superseded"] = "synthesized"
    title: str = Field(min_length=1)
    synthesis_rationale: str = Field(min_length=1)
    promoted_to: KSUID | None = None
    promoted_at: datetime | None = None
    promotion_rejected_at: datetime | None = None
    promotion_rejected_reason: str | None = None
    topics: list[str] = Field(default_factory=list)
    promotion_attempts: int = Field(default=0, ge=0)
    last_reinforced_at: datetime | None = None
    # Derived from `last_reinforced_at` by the validator; null when the
    # concept has never been reinforced. Kept as a real field (not a
    # computed property) so it lands in the Qdrant payload and can drive
    # scroll filters — mirrors the `created_epoch` / `updated_epoch`
    # pattern on MemoryObject.
    last_reinforced_epoch: float | None = None

    @model_validator(mode="after")
    def _normalise_and_guard(self) -> SynthesizedConcept:
        from musubi.types.common import epoch_of

        if self.promoted_at is not None:
            object.__setattr__(self, "promoted_at", ensure_utc(self.promoted_at))
        if self.promotion_rejected_at is not None:
            object.__setattr__(
                self,
                "promotion_rejected_at",
                ensure_utc(self.promotion_rejected_at),
            )
        if self.last_reinforced_at is not None:
            object.__setattr__(self, "last_reinforced_at", ensure_utc(self.last_reinforced_at))
            if self.last_reinforced_epoch is None:
                object.__setattr__(self, "last_reinforced_epoch", epoch_of(self.last_reinforced_at))

        if self.state == "promoted" and (self.promoted_to is None or self.promoted_at is None):
            raise ValueError("state=promoted requires promoted_to and promoted_at to be set")
        if self.promoted_to is not None and self.promoted_at is None:
            raise ValueError("promoted_to set but promoted_at missing")
        if self.promotion_rejected_at is not None and not self.promotion_rejected_reason:
            raise ValueError("promotion_rejected_at set but promotion_rejected_reason is empty")

        return self


__all__ = ["SynthesizedConcept"]
