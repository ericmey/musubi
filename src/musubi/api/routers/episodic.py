"""Episodic-memory read endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from qdrant_client import QdrantClient

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_episodic_plane, get_qdrant_client
from musubi.api.errors import APIError
from musubi.api.pagination import Page
from musubi.api.routers._scroll import scroll_namespace
from musubi.planes.episodic import EpisodicPlane
from musubi.types.episodic import EpisodicMemory

router = APIRouter(prefix="/v1/memories", tags=["episodic"])


@router.get(
    "/{object_id}",
    response_model=EpisodicMemory,
    dependencies=[Depends(require_auth())],
)
async def get_memory(
    object_id: str,
    namespace: str = Query(...),
    plane: EpisodicPlane = Depends(get_episodic_plane),
) -> EpisodicMemory:
    fetched = await plane.get(namespace=namespace, object_id=object_id)
    if fetched is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"episodic memory {object_id!r} not found in namespace {namespace!r}",
        )
    return fetched


@router.get(
    "",
    response_model=Page[EpisodicMemory],
    dependencies=[Depends(require_auth())],
)
async def list_memories(
    namespace: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = Query(None),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> Page[EpisodicMemory]:
    items, next_cursor = scroll_namespace(
        qdrant,
        collection="musubi_episodic",
        namespace=namespace,
        limit=limit,
        cursor=cursor,
    )
    return Page[EpisodicMemory](
        items=[EpisodicMemory.model_validate(p) for p in items],
        next_cursor=next_cursor,
    )


__all__ = ["router"]
