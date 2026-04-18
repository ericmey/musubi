"""``Thought`` — ambient messages between presences.

Preserved from the POC. Thoughts are messages, not knowledge — no lineage.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from musubi.types.base import MusubiObject


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


__all__ = ["Thought"]
