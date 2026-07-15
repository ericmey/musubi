"""``SourceArtifact`` and ``ArtifactChunk``.

Artifacts are immutable by design; chunks live inside an artifact's lifecycle
and are not independently versioned.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from musubi.types.base import MusubiObject
from musubi.types.common import KSUID, ArtifactIndexingState


class SourceArtifact(MusubiObject):
    """A raw uploaded file (PDF, VTT, markdown dump, etc.).

    ``state`` tracks the *lifecycle* axis (archived/matured), while
    ``artifact_state`` tracks the *indexing* axis (indexing/indexed/failed). The
    two are orthogonal — an artifact is typically ``state=matured`` its whole
    life and moves only on the indexing axis.
    """

    state: Literal["matured", "archived", "superseded"] = "matured"
    title: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex digest of the raw bytes.",
    )
    content_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    chunk_count: int = Field(ge=0, default=0)
    ingestion_metadata: dict[str, object] = Field(default_factory=dict)
    chunker: str = Field(
        min_length=1,
        description="Name of the chunker used, e.g. 'markdown-headings-v1'.",
    )
    artifact_state: ArtifactIndexingState = "indexing"
    failure_reason: str | None = None

    # -- C4 / ART-001 committed-generation head (additive) ---------------------------------------
    # The head is the single source of truth for which staged chunk generation is *committed*.
    # ``committed_generation`` + ``committed_owner`` are NEVER-REUSED tokens (the ABA fence); a
    # reader resolves the head first and then exposes only chunks whose (generation, owner_token)
    # equal these. ``publication_version`` is the monotonic OCC fence for the conditional head
    # publish; ``index_operation_id`` links the head to its durable indexing intent.
    committed_generation: str | None = None
    committed_owner: str | None = None
    index_operation_id: str | None = None
    publication_version: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def _indexing_axis_invariants(self) -> SourceArtifact:
        if self.artifact_state == "failed" and not self.failure_reason:
            raise ValueError("artifact_state=failed requires failure_reason to be set")
        if self.artifact_state == "indexed" and self.chunk_count < 1:
            raise ValueError("artifact_state=indexed requires chunk_count >= 1")
        # C4 invariant #1: an ``indexed`` head names exactly one committed generation+owner. This is
        # re-tightened to a hard requirement once the committed-generation indexer replaces the legacy
        # unfenced ``index()`` producer (this slice); enforcing it before that migration would reject
        # the legacy path's own tests. Tracked in the slice work log.
        return self


class ArtifactChunk(BaseModel):
    """One chunk of a source artifact. Not a MusubiObject — lives inside one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: KSUID
    artifact_id: KSUID
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)
    chunk_metadata: dict[str, object] = Field(default_factory=dict)
    # C4 / ART-001 staging fence (additive; None on legacy generation-less chunks, which are
    # rebuilt from the canonical blob — never guessed into a generation). A chunk is *committed*
    # (visible) only when its (generation, owner_token) equal the head's committed pair.
    generation: str | None = None
    owner_token: str | None = None

    @model_validator(mode="after")
    def _offsets_ordered(self) -> ArtifactChunk:
        if self.end_offset < self.start_offset:
            raise ValueError(f"end_offset ({self.end_offset}) < start_offset ({self.start_offset})")
        return self


__all__ = ["ArtifactChunk", "SourceArtifact"]
