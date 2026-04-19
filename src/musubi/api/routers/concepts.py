"""Synthesized-concept read endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from qdrant_client import QdrantClient

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_concept_plane, get_qdrant_client
from musubi.api.errors import APIError
from musubi.api.pagination import Page
from musubi.api.routers._scroll import scroll_namespace
from musubi.planes.concept import ConceptPlane
from musubi.types.concept import SynthesizedConcept

router = APIRouter(prefix="/v1/concepts", tags=["concept"])


@router.get(
    "/{object_id}",
    response_model=SynthesizedConcept,
    dependencies=[Depends(require_auth())],
)
async def get_concept(
    object_id: str,
    namespace: str = Query(...),
    plane: ConceptPlane = Depends(get_concept_plane),
) -> SynthesizedConcept:
    fetched = await plane.get(namespace=namespace, object_id=object_id)
    if fetched is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"concept {object_id!r} not found in namespace {namespace!r}",
        )
    return fetched


@router.get(
    "",
    response_model=Page[SynthesizedConcept],
    dependencies=[Depends(require_auth())],
)
async def list_concepts(
    namespace: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = Query(None),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> Page[SynthesizedConcept]:
    items, next_cursor = scroll_namespace(
        qdrant,
        collection="musubi_concept",
        namespace=namespace,
        limit=limit,
        cursor=cursor,
    )
    return Page[SynthesizedConcept](
        items=[SynthesizedConcept.model_validate(p) for p in items],
        next_cursor=next_cursor,
    )


__all__ = ["router"]
