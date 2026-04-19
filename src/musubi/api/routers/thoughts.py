"""Thought read endpoints — check (unread) + history (semantic search).

Both are POST endpoints per [[07-interfaces/canonical-api]] §5 (the body
carries query parameters). They're reads in disguise — no state mutation
— and live on the read surface per the slice-api-v0 split.

These endpoints take ``namespace`` in the request body, not the query
string, so they validate scope manually after body parse rather than
through the query-param-based ``require_auth`` dependency.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.responses import ThoughtListResponse
from musubi.api.routers._scroll import scroll_namespace
from musubi.auth import AuthRequirement, authenticate_request
from musubi.settings import Settings
from musubi.types.common import Err

router = APIRouter(prefix="/v1/thoughts", tags=["thoughts"])


class ThoughtCheckRequest(BaseModel):
    namespace: str
    presence: str
    limit: int = 50


class ThoughtHistoryRequest(BaseModel):
    namespace: str
    presence: str
    query_text: str
    limit: int = 20


def _check_body_scope(request: Request, namespace: str, settings: Settings) -> None:
    """Validate that the bearer's scope grants ``r`` on ``namespace``."""
    requirement = AuthRequirement(namespace=namespace, access="r")
    result = authenticate_request(
        request,  # type: ignore[arg-type]
        requirement,
        settings=settings,
    )
    if isinstance(result, Err):
        err = result.error
        code: ErrorCode = err.code  # type: ignore[assignment]
        raise APIError(
            status_code=err.status_code,
            code=code,
            detail=err.detail,
        )


@router.post("/check", response_model=ThoughtListResponse)
async def check_thoughts(
    request: Request,
    body: ThoughtCheckRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> ThoughtListResponse:
    _check_body_scope(request, body.namespace, settings)
    items, _ = scroll_namespace(
        qdrant,
        collection="musubi_thought",
        namespace=body.namespace,
        limit=body.limit,
        cursor=None,
    )
    return ThoughtListResponse(items=items)


@router.post("/history", response_model=ThoughtListResponse)
async def thought_history(
    request: Request,
    body: ThoughtHistoryRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> ThoughtListResponse:
    """First-cut: history is a namespace scroll. Semantic search will
    land once slice-retrieval-fast wires its dense path through the API
    in slice-api-v0-write."""
    _check_body_scope(request, body.namespace, settings)
    items, _ = scroll_namespace(
        qdrant,
        collection="musubi_thought",
        namespace=body.namespace,
        limit=body.limit,
        cursor=None,
    )
    return ThoughtListResponse(items=items)


__all__ = ["router"]
