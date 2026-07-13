"""Curated-knowledge write endpoints — POST / PATCH / DELETE."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from musubi.api.auth import authorize_namespace, require_auth
from musubi.api.dependencies import get_curated_plane, get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError
from musubi.api.idempotency_dependency import IdempotentContext, make_idempotency_dependency
from musubi.api.patch_guard import assert_readable_after_patch, reject_unknown_fields
from musubi.api.write_auth import AuthorizedWrite
from musubi.lifecycle.transitions import transition
from musubi.planes.curated import CuratedPlane
from musubi.settings import Settings
from musubi.types.common import Ok
from musubi.types.curated import CuratedKnowledge

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


async def authorized_curated_create(
    request: Request,
    body: CuratedCreateRequest = Body(...),
    settings: Settings = Depends(get_settings_dep),
) -> AuthorizedWrite[CuratedCreateRequest]:
    authorize_namespace(request, body.namespace, settings=settings, access="w")
    return AuthorizedWrite(auth=request.state.auth, namespace=body.namespace, body=body)


# Routed post-authz idempotency edge (see writes_episodic for the full rationale).
_idem_curated_create = make_idempotency_dependency(authorized_curated_create)


@router.post(
    "",
    response_model=CuratedCreateResponse,
    status_code=202,
    operation_id="create_curated.bucket=capture",
)
async def create_curated(
    ctx: IdempotentContext = Depends(_idem_curated_create),
    plane: CuratedPlane = Depends(get_curated_plane),
) -> CuratedCreateResponse:
    body = ctx.body  # single parsed body; namespace already authorized by the dependency edge
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
    # `exclude_unset`, NOT `exclude_none`.
    #
    # `exclude_none=True` drops explicitly-supplied nulls BEFORE the allowlist and the
    # canonical merged-row guard ever see them. So `PATCH {"retracted_original": null}`
    # became `{}` — the allowlist saw no unknown key, nothing was written, and the endpoint
    # returned **200 OK**. A caller who sent an unknown field was told it succeeded.
    #
    # That is a FALSE SUCCESS: the handler reported success without applying the mutation and
    # without rejecting it — the exact defect this PR exists to remove, living inside the
    # guard written to prevent it. It also conflated "field omitted" with "field explicitly
    # set to null", which are different requests. (Yua, review of d5c7e0f.)
    #
    # `exclude_unset=True` preserves the caller's ACTUAL key set, so:
    #   - unknown keys are rejected whatever their value, null included;
    #   - known nulls are judged by the canonical persisted model, not silently discarded;
    #   - omitted fields stay omitted.
    incoming = body.model_dump(exclude_unset=True)
    overlap = _FORBIDDEN_PATCH_FIELDS & set(incoming)
    if overlap:
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"PATCH cannot modify state-managed fields: {sorted(overlap)}",
        )
    reject_unknown_fields(incoming, _PATCHABLE_FIELDS, plane="curated")

    # Read RAW so that READING does not itself blow up on an already-corrupted row.
    # NOT a repair path — see musubi.api.patch_guard: this prevents further corruption, it
    # cannot heal an existing one (PATCH cannot remove an unknown key).
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
