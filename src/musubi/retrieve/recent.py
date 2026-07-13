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


def _provenance_score_for(plane: str, state: LifecycleState | None) -> float | None:
    """Exact-table-only lookup for recent-mode `provenance_score`.

    Per spec §3.3 (Yua 2026-07-13 09:49:53 #7): recent's `provenance_score`
    is exact-table-only. Returns `None` when:
      - `state is None` (legacy row without lifecycle state)
      - `(plane, state)` is absent from the explicit lookup table

    DOES NOT call `scoring._provenance` (which floors unknowns to 0.1).
    The ranked branch continues to use `scoring._provenance` for the
    actual `extra.score_components["provenance"]` value (which may
    legitimately be 0.1 for `_LOW_PROVENANCE_STATES`).

    Args:
        plane: the row's source plane (e.g. "episodic", "curated").
        state: the row's source LifecycleState, or None for legacy.

    Returns:
        The explicit table value if the pair is found; None otherwise.
    """
    if state is None:
        return None
    # Lazy import to avoid an import cycle: scoring imports types
    # which import common; recent only needs the table constant.
    from musubi.retrieve.scoring import _PROVENANCE

    return _PROVENANCE.get((plane, state))


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

    # IMPORTANT — `order_by` on Qdrant scroll requires the key to be a
    # declared payload index on the target collection. `created_epoch`
    # is indexed on every plane that uses it for ordering (verified via
    # `musubi.planes.thoughts.plane` which already scrolls with the same
    # `order_by=created_epoch DESC`). New planes that want recent-mode
    # support must declare the index at collection creation; otherwise
    # this call fails at runtime with a Qdrant error. `order_by` is also
    # incompatible with offset-based pagination — current code asks for
    # the first `capped_limit` rows only, which is the supported shape.
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
            # Surfacing the skip as DEBUG (not WARN) — for recent mode
            # this is more likely to bite than ranked modes ("give me
            # everything since X" callers expect every row), but a row
            # with NO payload at all has nothing to project. The DEBUG
            # log lets operators investigate data-quality drift without
            # being woken up.
            logger.debug(
                "recent retrieve skipping empty-payload point id=%s",
                record.id,
            )
            continue
        # Explicit `is None` rather than `or 0.0` so a legitimately old
        # row with `created_epoch=0.0` isn't indistinguishable from a
        # missing field. A row missing `created_epoch` is a data-quality
        # signal worth logging; the score still falls back to 0.0 so the
        # row isn't dropped.
        raw_ce = payload.get("created_epoch")
        if raw_ce is None:
            logger.debug(
                "recent retrieve row missing created_epoch object_id=%s",
                payload.get("object_id") or record.id,
            )
            created_epoch = 0.0
        else:
            created_epoch = float(raw_ce)
        hits.append(
            RecentHit(
                object_id=str(payload.get("object_id") or record.id),
                payload=payload,
                snippet=_snippet(payload),
                created_epoch=created_epoch,
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
