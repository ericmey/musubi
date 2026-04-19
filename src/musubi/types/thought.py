"""``Thought`` — ambient messages between presences.

Preserved from the POC: a Thought is a message, not a memory. It carries
minimal lineage (``in_reply_to`` / ``supersedes``) per the
[[04-data-model/thoughts]] spec so a reply-chain or a correction-chain can
be walked, but it is not a first-class MemoryObject (no embeddings tracked
here, no reinforcement, no maturation beyond ``provisional → matured →
archived``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from musubi.types.base import MusubiObject
from musubi.types.common import KSUID


class Thought(MusubiObject):
    """A message from one presence to another (or to 'all')."""

    state: Literal["provisional", "matured", "archived"] = "provisional"
    content: str = Field(min_length=1)
    from_presence: str = Field(min_length=1)
    to_presence: str = Field(
        min_length=1,
        description="A concrete presence identifier or the literal string 'all'.",
    )
    read: bool = False
    read_by: list[str] = Field(default_factory=list)
    channel: str = "default"
    importance: int = Field(ge=1, le=10, default=5)

    # Lineage (rare, but supported — see 04-data-model/thoughts.md §Pydantic model).
    # ``in_reply_to`` links a reply to its parent thought; ``supersedes`` lists
    # prior thought object_ids this one replaces (e.g. a correction to an
    # earlier broadcast). Both default to empty so the common case is unaffected.
    in_reply_to: KSUID | None = None
    supersedes: list[KSUID] = Field(default_factory=list)


__all__ = ["Thought"]
