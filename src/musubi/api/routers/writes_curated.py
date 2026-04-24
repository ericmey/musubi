"""Curated-knowledge write endpoints — POST / PATCH / DELETE."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_curated_plane, get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.auth import AuthRequirement, authenticate_request
from musubi.lifecycle.transitions import transition
from musubi.planes.curated import CuratedPlane
from musubi.settings import Settings
from musubi.types.common import Err, Ok
from musubi.types.curated import CuratedKnowledge


def _check_body_scope(request: Request, namespace: str, settings: Settings) -> None:
    requirement = AuthRequirement(namespace=namespace, access="w")
    result = authenticate_request(
        request,  # type: ignore[arg-type]
        requirement,
        settings=settings,
    )
    if isinstance(result, Err):
        err = result.error
        code: ErrorCode = err.code  # type: ignore[assignment]
        raise APIError(status_code=err.status_code, code=code, detail=err.detail)


router = APIRouter(prefix="/v1/curated", tags=["curated-writes"])


class CuratedCreateRequest(BaseModel):
    namespace: str
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    summary: str | None = None
    vault_path: str = Field(min_length=1)
    body_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    topics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=7, ge=1, le=10)


class CuratedCreateResponse(BaseModel):
    object_id: str
    state: str


class PatchCuratedRequest(BaseModel):
    """Non-state metadata only — same convention as PATCH /v1/episodic.

    ``extra="allow"`` so the handler can return a typed BAD_REQUEST
    naming the forbidden field instead of a generic 422."""

    model_config = ConfigDict(extra="allow")

    tags: list[str] | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    topics: list[str] | None = None


_FORBIDDEN_PATCH_FIELDS = {"state", "version", "object_id", "namespace", "vault_path"}


@router.post(
    "",
    response_model=CuratedCreateResponse,
    status_code=202,
    operation_id="create_curated.bucket=capture",
)
async def create_curated(
    request: Request,
    body: CuratedCreateRequest = Body(...),
    plane: CuratedPlane = Depends(get_curated_plane),
    settings: Settings = Depends(get_settings_dep),
) -> CuratedCreateResponse:
    _check_body_scope(request, body.namespace, settings)
    saved = await plane.create(
        CuratedKnowledge(
            namespace=body.namespace,
            title=body.title,
            content=body.content,
            summary=body.summary,
            vault_path=body.vault_path,
            body_hash=body.body_hash,
            topics=body.topics,
            tags=body.tags,
            importance=body.importance,
        )
    )
    return CuratedCreateResponse(object_id=saved.object_id, state=saved.state)


@router.patch(
    "/{object_id}",
    response_model=CuratedKnowledge,
    operation_id="patch_curated.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def patch_curated(
    object_id: str,
    namespace: str = Query(...),
    body: PatchCuratedRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: CuratedPlane = Depends(get_curated_plane),
) -> CuratedKnowledge:
    incoming = body.model_dump(exclude_none=True)
    overlap = _FORBIDDEN_PATCH_FIELDS & set(incoming)
    if overlap:
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"PATCH cannot modify state-managed fields: {sorted(overlap)}",
        )
    current = await plane.get(namespace=namespace, object_id=object_id)
    if current is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"curated knowledge {object_id!r} not found in namespace {namespace!r}",
        )
    qdrant.set_payload(
        collection_name="musubi_curated",
        payload=incoming,
        points=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id)),
            ]
        ),
    )
    refreshed = await plane.get(namespace=namespace, object_id=object_id)
    assert refreshed is not None
    return refreshed


@router.delete(
    "/{object_id}",
    operation_id="delete_curated.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def delete_curated(
    object_id: str,
    namespace: str = Query(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: CuratedPlane = Depends(get_curated_plane),
) -> Response:
    current = await plane.get(namespace=namespace, object_id=object_id)
    if current is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"curated knowledge {object_id!r} not found in namespace {namespace!r}",
        )
    result = transition(
        qdrant,
        object_id=object_id,
        target_state="archived",
        actor="api-delete",
        reason="api-soft-delete",
    )
    if not isinstance(result, Ok):
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"delete transition rejected: {result.error.message}",
        )
    return Response(
        status_code=200, content=b'{"status":"archived"}', media_type="application/json"
    )


__all__ = ["router"]
