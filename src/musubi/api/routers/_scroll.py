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

from musubi.store.names import collection_for_plane
from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD, strip_layout_fields

log = logging.getLogger(__name__)

# Collections with the DATA-001 P2 multi-point layout — a listed row must be resolved anchor-over-content
# and fail closed on a dangling/cross-object pointer, never returned as a raw (possibly invalid) anchor.
_IMMUTABLE_VECTOR_COLLECTIONS = frozenset(
    {collection_for_plane("episodic"), collection_for_plane("curated")}
)

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

    DATA-001 P2: pages IDENTITY rows only (excludes content snapshots). For episodic/curated the anchor
    is not returned raw — each row is resolved anchor-over-content and FAILS CLOSED (dropped) on a
    dangling/cross-object pointer, so a corrupt row is never listed with invalid committed content. A
    corrupt row underfills the page WITHOUT changing ``next_offset`` (pagination truth is preserved).
    concept/thought/artifact rows are returned raw (no anchors).
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
            scroll_filter=models.Filter(
                must=list(must_conditions),
                must_not=[  # identity rows only — never a write-once content snapshot
                    models.FieldCondition(
                        key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                    )
                ],
            ),
            limit=limit,
            offset=offset,  # type: ignore[arg-type]
            with_payload=True,
        )
    except Exception as exc:
        log.warning("api-scroll-failed collection=%s err=%r", collection, exc)
        return [], None

    next_cursor = _encode_offset(next_offset) if next_offset is not None else None
    if collection not in _IMMUTABLE_VECTOR_COLLECTIONS:
        return [dict(r.payload) for r in records if r.payload], next_cursor

    from musubi.store.immutable_vectors import resolve_committed_content

    items: list[dict[str, Any]] = []
    for r in records:
        if not r.payload:
            continue
        object_id = r.payload.get("object_id")
        if object_id is None:
            continue
        resolved = resolve_committed_content(
            client, collection, namespace=namespace, object_id=str(object_id)
        )
        if resolved is None:
            continue  # dangling/cross-object committed pointer -> fail closed (underfill, keep offset).
        items.append(strip_layout_fields(resolved))
    return items, next_cursor


__all__ = ["scroll_namespace"]
