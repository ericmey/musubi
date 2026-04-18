"""``CuratedKnowledge`` — what we believe is true.

Source of truth is the Obsidian vault; Qdrant mirrors. Per the lifecycle spec,
curated objects start in ``matured``; they never pass through ``provisional``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from musubi.types.base import MemoryObject
from musubi.types.common import KSUID, LifecycleState, ensure_utc

_CURATED_STATES: frozenset[LifecycleState] = frozenset({"matured", "superseded", "archived"})


class CuratedKnowledge(MemoryObject):
    """A curated, human-authored knowledge note mirrored from the vault.

    ``vault_path`` points at the ``.md`` file in the vault; ``body_hash`` is the
    sha256 of the rendered markdown body (frontmatter excluded) — used by the
    watcher to detect real edits vs. metadata-only churn.
    """

    state: Literal["matured", "superseded", "archived"] = "matured"
    title: str = Field(min_length=1)
    topics: list[str] = Field(default_factory=list)
    vault_path: str = Field(min_length=1)
    body_hash: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex digest of the rendered markdown body.",
    )
    musubi_managed: bool = True
    promoted_from: KSUID | None = None
    promoted_at: datetime | None = None

    @model_validator(mode="after")
    def _normalise_optional_times(self) -> CuratedKnowledge:
        if self.promoted_at is not None:
            object.__setattr__(self, "promoted_at", ensure_utc(self.promoted_at))
        if self.promoted_from is not None and self.promoted_at is None:
            raise ValueError(
                "promoted_at must be set whenever promoted_from is set "
                "(promotion is a single event — provenance without a timestamp is useless)"
            )
        return self


__all__ = ["CuratedKnowledge"]
