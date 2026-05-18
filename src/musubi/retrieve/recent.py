"""Time-ordered scroll retrieval — the backend for ``musubi_recent``.

Per [[_slices/slice-retrieve-recent]]. Pure Qdrant scroll, ordered by
``created_epoch`` DESC, filtered by namespace + state + optional ``since``
range + optional ``tags``. **No embedder. No rerank.**

Per-target callable (mirrors :func:`musubi.retrieve.fast.run_fast_retrieve`)
so the orchestrator's existing wildcard / cross-plane fanout invokes this
once per ``(namespace, collection)`` target unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.types.common import Err, LifecycleState, Namespace, Ok, Result

logger = logging.getLogger(__name__)

# Recent mode includes `provisional` by default — different from fast/deep
# (which default to matured+promoted). Recent is "what just happened";
# excluding the freshest tier defeats the use case. Spec'd in
# [[_slices/slice-retrieve-recent]] design-decisions section.
_DEFAULT_STATES: tuple[LifecycleState, ...] = ("provisional", "matured", "promoted")

# Hard server-side cap. Matches the cap convention across other retrieve
# modes; callers requesting higher are silently clamped.
_MAX_LIMIT = 50


@dataclass(frozen=True, slots=True)
class RecentRetrievalError:
    """Typed error returned by the recent retrieval boundary."""

    code: str
    detail: str
    status_code: int


@dataclass(frozen=True, slots=True)
class RecentHit:
    """One time-ordered retrieval hit."""

    object_id: str
    payload: dict[str, Any]
    snippet: str
    created_epoch: float


@dataclass(frozen=True, slots=True)
class RecentRetrieveResult:
    """Successful recent retrieval response."""

    results: list[RecentHit]
    status_code: int = 200


async def run_recent_retrieve(
    client: QdrantClient,
    *,
    namespace: Namespace,
    collection: str,
    limit: int = 10,
    since: float | None = None,
    tags: Sequence[str] | None = None,
    state_filter: Sequence[LifecycleState] | None = None,
) -> Result[RecentRetrieveResult, RecentRetrievalError]:
    """Scroll ``collection`` filtered by ``namespace``, ordered newest-first.

    No embedder call. No rerank call. Server-side cap of ``_MAX_LIMIT``.
    ``since`` is interpreted as inclusive epoch seconds (``created_epoch >=
    since``). ``tags`` filter is AND across all entries — a row must
    contain every listed tag to match.
    """
    if limit <= 0:
        return Err(
            error=RecentRetrievalError(
                code="invalid_limit",
                detail="limit must be positive",
                status_code=400,
            )
        )
    capped_limit = min(limit, _MAX_LIMIT)
    states = tuple(state_filter) if state_filter else _DEFAULT_STATES

    # `must` is typed broadly because qdrant_client.models.Filter accepts a
    # union of condition kinds (FieldCondition, IsEmptyCondition, etc.) and
    # the list-invariance rule would reject a narrower `list[FieldCondition]`
    # at the Filter call site.
    must: list[Any] = [
        models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
        models.FieldCondition(key="state", match=models.MatchAny(any=[str(s) for s in states])),
    ]
    if since is not None:
        must.append(models.FieldCondition(key="created_epoch", range=models.Range(gte=since)))
    # Tags filter: AND across all listed tags. Each tag is its own
    # FieldCondition on the array — Qdrant treats array-key match as
    # "contains this value", so AND-ing them yields "contains every".
    if tags:
        for tag in tags:
            must.append(models.FieldCondition(key="tags", match=models.MatchValue(value=tag)))

    try:
        records, _ = client.scroll(
            collection_name=collection,
            scroll_filter=models.Filter(must=must),
            limit=capped_limit,
            with_payload=True,
            order_by=models.OrderBy(
                key="created_epoch",
                direction=models.Direction.DESC,
            ),
        )
    except Exception as exc:
        logger.error(
            "recent retrieve scroll failed (collection=%s namespace=%s): %s",
            collection,
            namespace,
            exc,
            exc_info=True,
        )
        return Err(
            error=RecentRetrievalError(
                code="index_unavailable",
                detail=str(exc),
                status_code=503,
            )
        )

    hits: list[RecentHit] = []
    for record in records:
        payload = dict(record.payload or {})
        if not payload:
            continue
        hits.append(
            RecentHit(
                object_id=str(payload.get("object_id") or record.id),
                payload=payload,
                snippet=_snippet(payload),
                created_epoch=float(payload.get("created_epoch") or 0.0),
            )
        )

    return Ok(value=RecentRetrieveResult(results=hits))


def _snippet(payload: dict[str, Any], max_chars: int = 300) -> str:
    content = str(payload.get("content") or payload.get("title") or "")
    return content[:max_chars]


__all__ = [
    "RecentHit",
    "RecentRetrievalError",
    "RecentRetrieveResult",
    "run_recent_retrieve",
]
