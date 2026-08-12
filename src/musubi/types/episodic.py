"""``EpisodicMemory`` — what happened.

Primary write target for adapters (MCP, LiveKit, etc.). States allowed:
``provisional``, ``matured``, ``demoted``, ``archived``, ``superseded``.
See [[04-data-model/lifecycle#EpisodicMemory]] for the transition diagram.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from musubi.types.base import MemoryObject
from musubi.types.common import (
    ArtifactRef,
    LifecycleState,
    Modality,
    Namespace,
    ensure_utc,
    utc_now,
)

_EPISODIC_STATES: frozenset[LifecycleState] = frozenset(
    {"provisional", "matured", "demoted", "archived", "superseded"}
)

_SHA256_HEX = r"^[0-9a-f]{64}$"


class RetractionArtifactRef(ArtifactRef):
    """Whole-artifact escrow reference; chunk and quote ambiguity is unrepresentable."""

    chunk_id: None = Field(default=None, exclude=True)
    quote: None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _forbid_nonwhole_reference_keys(cls, value: object) -> object:
        if isinstance(value, Mapping):
            present = [key for key in ("chunk_id", "quote") if key in value]
            if present:
                raise ValueError(
                    "whole-artifact retraction reference forbids " + ", ".join(present)
                )
        return value


class V2RetractionPointer(BaseModel):
    """The immutable content pointer preserved by a v2 anchor retraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["v2"]
    live_point: str = Field(min_length=1)


class LegacyRetractionPointer(BaseModel):
    """Explicit marker for a legacy single-point row retaining its own vector."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["legacy_self"]


type RetractionPointer = Annotated[
    V2RetractionPointer | LegacyRetractionPointer,
    Field(discriminator="kind"),
]


class RetractionEvidence(BaseModel):
    """Strict public evidence for one escrow-backed non-reembedding retraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["artifact_escrow_v1"]
    artifact_namespace: Namespace
    artifact_ref: RetractionArtifactRef
    original_sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256_HEX)
    original_utf8_bytes: int = Field(ge=1)
    quoted_prefix_utf8_bytes: int = Field(ge=1)
    omitted_bytes: int = Field(ge=0)
    vector_basis: Literal["original"]
    preserved_pointer: RetractionPointer
    operation_identity_hash: str = Field(min_length=64, max_length=64, pattern=_SHA256_HEX)
    request_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256_HEX)

    @model_validator(mode="after")
    def _reconcile_byte_accounting(self) -> RetractionEvidence:
        if self.quoted_prefix_utf8_bytes + self.omitted_bytes != self.original_utf8_bytes:
            raise ValueError(
                "quoted_prefix_utf8_bytes plus omitted_bytes must equal original_utf8_bytes"
            )
        return self


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
    # Real stored field, NOT a computed property, deliberately mirroring the
    # created_epoch / updated_epoch pattern on MemoryObject: the value must
    # round-trip through Qdrant payloads under `extra="forbid"`. (As a plain
    # @property it was absent from the model schema, so the indexed
    # `importance_last_scored_epoch` payload key could never be written
    # without making the row fail validation on every subsequent read.)
    # Derived from `importance_last_scored_at` when omitted — see
    # `_normalise_times`.
    importance_last_scored_epoch: float | None = None
    retraction_evidence: RetractionEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _normalise_times(self) -> EpisodicMemory:
        from musubi.types.common import epoch_of

        object.__setattr__(self, "event_at", ensure_utc(self.event_at))
        object.__setattr__(self, "ingested_at", ensure_utc(self.ingested_at))
        if self.importance_last_scored_at is not None:
            object.__setattr__(
                self, "importance_last_scored_at", ensure_utc(self.importance_last_scored_at)
            )
            if self.importance_last_scored_epoch is None:
                object.__setattr__(
                    self,
                    "importance_last_scored_epoch",
                    epoch_of(self.importance_last_scored_at),
                )
        if self.retraction_evidence is not None:
            family, presence, _plane = self.namespace.split("/")
            sibling = f"{family}/{presence}/artifact"
            if self.retraction_evidence.artifact_namespace != sibling:
                raise ValueError(
                    "retraction_evidence artifact_namespace must be the derived sibling "
                    "artifact namespace"
                )
        return self


__all__ = [
    "EpisodicMemory",
    "LegacyRetractionPointer",
    "RetractionArtifactRef",
    "RetractionEvidence",
    "RetractionPointer",
    "V2RetractionPointer",
]
