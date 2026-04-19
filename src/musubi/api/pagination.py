"""Cursor-paginated response envelope shape.

Per [[07-interfaces/canonical-api]] § Pagination, list endpoints return
``{items: [...], next_cursor: <opaque-string-or-null>}``. Cursors are
opaque to the client — clients treat them as round-trip tokens.

The cursor encode/decode is implemented in
:mod:`musubi.api.routers._scroll` (it wraps Qdrant's native scroll
``next_offset``); this module exposes only the response envelope so
every list router returns the same shape.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Page(BaseModel, Generic[T]):
    """Generic page envelope returned by every list endpoint."""

    items: list[T]
    next_cursor: str | None = None


__all__ = ["Page"]
