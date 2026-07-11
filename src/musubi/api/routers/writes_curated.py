"""Curated-knowledge write endpoints — POST / PATCH / DELETE."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_curated_plane, get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.patch_guard import assert_readable_after_patch, reject_unknown_fields
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

# Derived from the model, never hand-maintained. See PATCH /v1/episodic for the full
# reasoning: the body is `extra="allow"` and `incoming` is written verbatim by
# `set_payload`, so a DENYLIST of five names could not stop an unmodeled key from
# reaching the payload — where the READ model (`extra="forbid"`) rejects it forever,
# making the row unreadable AND (before this PR) undeletable.
#
# Curated is the shared settled-truth plane. A denylist guarding it can only block the
# mistakes someone already imagined; everything else becomes permanent false ground.
_PATCHABLE_FIELDS = set(PatchCuratedRequest.model_fields)


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
    reject_unknown_fields(incoming, _PATCHABLE_FIELDS, plane="curated")

    # Read RAW so this still works on an already-corrupted row being repaired.
    current_raw = await plane.raw_payload(namespace=namespace, object_id=object_id)
    if current_raw is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"curated knowledge {object_id!r} not found in namespace {namespace!r}",
        )

    # NEVER PERSIST WHAT YOU CANNOT READ BACK. The allowlist stops unknown keys; this
    # stops invalid values of known keys. Curated is shared settled truth — a row bricked
    # here is permanent false ground for every agent. See musubi.api.patch_guard.
    assert_readable_after_patch(current_raw, incoming, CuratedKnowledge, object_id=object_id)
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
    # exists(), not get(): the transition below goes by object_id and never touches the
    # deserialized row, so a corrupted payload must not be able to block the archive.
    # Curated is shared settled truth — a false row here that cannot be archived out of
    # the way keeps teaching every agent that reads the plane.
    if not await plane.exists(namespace=namespace, object_id=object_id):
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
