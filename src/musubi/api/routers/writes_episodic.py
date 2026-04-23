"""Episodic write endpoints — POST capture / batch / PATCH / DELETE."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_episodic_plane, get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.auth import AuthRequirement, authenticate_request
from musubi.lifecycle.transitions import transition
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.types.common import Err, Ok
from musubi.types.episodic import EpisodicMemory


def _check_body_scope(request: Request, namespace: str, settings: Settings) -> None:
    """Validate the bearer's scope grants ``w`` on a body-supplied namespace."""
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


def _require_operator_for_created_at(request: Request) -> None:
    """Guard the ``created_at`` override on capture endpoints.

    Overriding the created_at timestamp is an operator-only privilege:
    it lets the migration path preserve source-truth timestamps when
    ingesting historical data, but it must not be available to
    regular consumers because it would let a token rewrite when an
    event "happened". The bearer's ``AuthContext`` is already
    attached by the ``require_auth`` dependency on the route; if the
    scope list doesn't include ``operator`` we 403 before touching
    the plane."""
    ctx = getattr(request.state, "auth", None)
    if ctx is None or "operator" not in (ctx.scopes or ()):
        raise APIError(
            status_code=403,
            code="FORBIDDEN",
            detail=(
                "created_at override requires operator scope; pass an "
                "operator token or omit the field"
            ),
        )


router = APIRouter(prefix="/v1/memories", tags=["episodic-writes"])


class CaptureRequest(BaseModel):
    namespace: str
    content: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    # Optional migration / replay override — operator scope required (see
    # _require_operator_for_created_at below). Normal consumers omit this
    # and Musubi stamps created_at at ingest time via EpisodicMemory's
    # default factory.
    created_at: datetime | None = None


class CaptureResponse(BaseModel):
    object_id: str
    state: str
    dedup: dict[str, str] | None = None


class CaptureItem(BaseModel):
    """One row in a batch capture. Inherits ``namespace`` from the parent."""

    content: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=5, ge=1, le=10)
    # Per-item created_at override — operator scope required (checked
    # once on the outer batch before iterating).
    created_at: datetime | None = None


class BatchCaptureRequest(BaseModel):
    namespace: str
    items: list[CaptureItem]


class BatchCaptureResponse(BaseModel):
    object_ids: list[str]


class PatchEpisodicRequest(BaseModel):
    """Non-state field updates only. ``state`` mutations go through
    POST /v1/lifecycle/transition (the canonical primitive). Extra
    fields are captured (``extra="allow"``) so the handler can return
    a typed BAD_REQUEST naming the forbidden field instead of a
    generic 422."""

    model_config = ConfigDict(extra="allow")

    tags: list[str] | None = None
    importance: int | None = Field(default=None, ge=1, le=10)
    summary: str | None = None


_FORBIDDEN_PATCH_FIELDS = {"state", "version", "object_id", "namespace"}


@router.post(
    "",
    response_model=CaptureResponse,
    status_code=202,
    operation_id="capture_episodic.bucket=capture",
)
async def capture(
    request: Request,
    body: CaptureRequest = Body(...),
    plane: EpisodicPlane = Depends(get_episodic_plane),
    settings: Settings = Depends(get_settings_dep),
) -> CaptureResponse:
    _check_body_scope(request, body.namespace, settings)
    memory_kwargs: dict[str, object] = {
        "namespace": body.namespace,
        "content": body.content,
        "summary": body.summary,
        "tags": body.tags,
        "importance": body.importance,
    }
    preserve_created_at = False
    if body.created_at is not None:
        _require_operator_for_created_at(request)
        memory_kwargs["created_at"] = body.created_at
        preserve_created_at = True
    saved = await plane.create(
        EpisodicMemory(**memory_kwargs),  # type: ignore[arg-type]
        preserve_created_at=preserve_created_at,
    )
    response = CaptureResponse(object_id=saved.object_id, state=saved.state)
    request.state.idempotency_response = response.model_dump()
    return response


@router.post(
    "/batch",
    response_model=BatchCaptureResponse,
    status_code=202,
    operation_id="batch_capture.bucket=batch-write",
)
async def batch_capture(
    request: Request,
    body: BatchCaptureRequest = Body(...),
    plane: EpisodicPlane = Depends(get_episodic_plane),
    settings: Settings = Depends(get_settings_dep),
) -> BatchCaptureResponse:
    _check_body_scope(request, body.namespace, settings)
    # Check operator scope once up front if ANY item overrides
    # created_at. A batch with mixed override / no-override is fine
    # under operator scope; a batch with any override under a
    # non-operator token is a 403 for the whole batch (simpler to
    # reason about than per-item partial failures).
    if any(item.created_at is not None for item in body.items):
        _require_operator_for_created_at(request)
    out: list[str] = []
    for item in body.items:
        memory_kwargs: dict[str, object] = {
            "namespace": body.namespace,
            "content": item.content,
            "summary": item.summary,
            "tags": item.tags,
            "importance": item.importance,
        }
        preserve = False
        if item.created_at is not None:
            memory_kwargs["created_at"] = item.created_at
            preserve = True
        saved = await plane.create(
            EpisodicMemory(**memory_kwargs),  # type: ignore[arg-type]
            preserve_created_at=preserve,
        )
        out.append(saved.object_id)
    return BatchCaptureResponse(object_ids=out)


@router.patch(
    "/{object_id}",
    response_model=EpisodicMemory,
    operation_id="patch_episodic.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def patch_episodic(
    object_id: str,
    namespace: str = Query(...),
    body: PatchEpisodicRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: EpisodicPlane = Depends(get_episodic_plane),
) -> EpisodicMemory:
    """Update non-state metadata on an existing episodic row.

    State changes are forbidden on this surface — they go through
    POST /v1/lifecycle/transition so the lifecycle ledger records every
    state mutation. Tags / importance / summary are non-state metadata
    and land via a payload-only ``set_payload`` (analogous to the
    enrichment writes the maturation sweep does — see
    :mod:`musubi.lifecycle.maturation` for precedent).
    """
    incoming = body.model_dump(exclude_none=True)
    overlap = _FORBIDDEN_PATCH_FIELDS & set(incoming)
    if overlap:
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"PATCH cannot modify state-managed fields: {sorted(overlap)}; "
            f"use POST /v1/lifecycle/transition for state changes",
        )
    current = await plane.get(namespace=namespace, object_id=object_id)
    if current is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
        )
    qdrant.set_payload(
        collection_name="musubi_episodic",
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
    operation_id="delete_episodic.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def delete_episodic(
    request: Request,
    object_id: str,
    namespace: str = Query(...),
    hard: bool = Query(False),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: EpisodicPlane = Depends(get_episodic_plane),
) -> Response:
    """Soft-delete by default (state → archived via the canonical
    ``transition()`` primitive). ``?hard=true`` requires operator
    scope and removes the point from Qdrant entirely.

    The operator-scope check on the hard path reads the
    :class:`AuthContext` that the outer ``require_auth`` dependency has
    already attached to ``request.state.auth``."""
    if hard:
        ctx = getattr(request.state, "auth", None)
        if ctx is None or "operator" not in (ctx.scopes or ()):
            raise APIError(
                status_code=403,
                code="FORBIDDEN",
                detail="hard delete requires operator scope; pass an operator token",
            )
        current = await plane.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise APIError(
                status_code=404,
                code="NOT_FOUND",
                detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
            )
        from qdrant_client import models as _qm

        qdrant.delete(
            collection_name="musubi_episodic",
            points_selector=_qm.FilterSelector(
                filter=_qm.Filter(
                    must=[
                        _qm.FieldCondition(key="object_id", match=_qm.MatchValue(value=object_id)),
                    ]
                )
            ),
        )
        return Response(status_code=204)
    current = await plane.get(namespace=namespace, object_id=object_id)
    if current is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
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
