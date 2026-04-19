"""Contradictions list endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_qdrant_client
from musubi.api.responses import ContradictionListResponse, ContradictionPair

router = APIRouter(prefix="/v1/contradictions", tags=["contradictions"])


@router.get(
    "",
    response_model=ContradictionListResponse,
    dependencies=[Depends(require_auth())],
)
async def list_contradictions(
    namespace: str | None = Query(None),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> ContradictionListResponse:
    """Concept rows whose ``contradicts`` field is non-empty.

    Cross-namespace by default (operator scope); namespace-scoped when
    ``namespace`` is supplied. The data source is the concept plane
    payload — concepts mark each other as contradicting during synthesis
    (slice-lifecycle-synthesis).
    """
    scroll_filter: models.Filter | None = None
    if namespace is not None:
        scroll_filter = models.Filter(
            must=[
                models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
            ]
        )
    try:
        records, _ = qdrant.scroll(
            collection_name="musubi_concept",
            scroll_filter=scroll_filter,
            limit=200,
            with_payload=True,
        )
    except Exception:
        return ContradictionListResponse(items=[])

    pairs: list[ContradictionPair] = []
    for rec in records:
        if not rec.payload:
            continue
        contradicts = rec.payload.get("contradicts") or []
        if not contradicts:
            continue
        pairs.append(
            ContradictionPair(
                object_id=str(rec.payload.get("object_id", "")),
                contradicts=[str(c) for c in contradicts],
                namespace=str(rec.payload.get("namespace", "")),
            )
        )
    return ContradictionListResponse(items=pairs)


__all__ = ["router"]
