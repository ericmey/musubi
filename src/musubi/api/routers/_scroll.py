"""Shared cursor-paginated scroll helper for read list endpoints.

The plane modules expose ``get`` (single by id) and ``query`` (dense
search). They don't expose ``list_by_namespace`` because the planes are
write-side authorities; bulk-listing is a read-time concern that the API
adapts off of Qdrant's payload-filter scroll directly.

Pagination uses Qdrant's native ``scroll`` ``next_offset`` token wrapped
in our opaque cursor envelope (``c1.<base64>``) so clients see a stable
opaque string while the underlying pagination is whatever Qdrant
provides. This sidesteps tie-breaking issues with epoch-keyed cursors
when multiple rows share a ``created_epoch``.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from qdrant_client import QdrantClient, models

log = logging.getLogger(__name__)

_CURSOR_PREFIX = "c1."


def _encode_offset(offset: object) -> str:
    payload = json.dumps({"offset": offset}).encode("utf-8")
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_offset(cursor: str) -> object | None:
    if not cursor.startswith(_CURSOR_PREFIX):
        return None
    raw = cursor[len(_CURSOR_PREFIX) :]
    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
        offset: object | None = data.get("offset")
        return offset
    except (ValueError, json.JSONDecodeError):
        return None


def scroll_namespace(
    client: QdrantClient,
    *,
    collection: str,
    namespace: str,
    limit: int,
    cursor: str | None,
    extra_must: list[models.FieldCondition] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Scroll one page of payloads from ``collection`` filtered to
    ``namespace``. Pagination wraps Qdrant's native ``next_offset``.

    Returns ``(items, next_cursor)``. ``next_cursor`` is ``None`` when
    Qdrant signals that no more pages remain.
    """
    offset = _decode_offset(cursor) if cursor else None
    must_conditions: list[models.FieldCondition] = [
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
    ]
    if extra_must:
        must_conditions.extend(extra_must)

    try:
        records, next_offset = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(must=list(must_conditions)),
            limit=limit,
            offset=offset,  # type: ignore[arg-type]
            with_payload=True,
        )
    except Exception as exc:
        log.warning("api-scroll-failed collection=%s err=%r", collection, exc)
        return [], None

    items: list[dict[str, Any]] = [dict(r.payload) for r in records if r.payload]
    next_cursor = _encode_offset(next_offset) if next_offset is not None else None
    return items, next_cursor


__all__ = ["scroll_namespace"]
