"""Lifecycle transition endpoint — wraps the canonical primitive."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from musubi.api.auth import require_operator
from musubi.api.dependencies import get_qdrant_client
from musubi.api.errors import APIError
from musubi.lifecycle.transitions import LineageUpdates, transition
from musubi.types.common import Ok

router = APIRouter(prefix="/v1/lifecycle", tags=["lifecycle-writes"])


class TransitionRequest(BaseModel):
    object_id: str = Field(min_length=27, max_length=27)
    to_state: str
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    superseded_by: str | None = None
    supersedes: list[str] = Field(default_factory=list)


class TransitionResponseBody(BaseModel):
    object_id: str
    from_state: str
    to_state: str
    version: int


@router.post(
    "/transition",
    response_model=TransitionResponseBody,
    operation_id="lifecycle_transition.bucket=transition",
    dependencies=[Depends(require_operator())],
)
async def lifecycle_transition(
    body: TransitionRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> TransitionResponseBody:
    lineage: LineageUpdates | None = None
    if body.superseded_by or body.supersedes:
        lineage = LineageUpdates(
            superseded_by=body.superseded_by,
            supersedes=body.supersedes,
        )
    result = transition(
        qdrant,
        object_id=body.object_id,
        target_state=body.to_state,  # type: ignore[arg-type]
        actor=body.actor,
        reason=body.reason,
        lineage_updates=lineage,
    )
    if not isinstance(result, Ok):
        err = result.error
        if err.code == "not_found":
            raise APIError(status_code=404, code="NOT_FOUND", detail=err.message)
        if err.code == "illegal_transition":
            raise APIError(status_code=400, code="BAD_REQUEST", detail=err.message)
        raise APIError(status_code=400, code="BAD_REQUEST", detail=err.message)
    tr = result.value
    return TransitionResponseBody(
        object_id=tr.object_id,
        from_state=tr.from_state,
        to_state=tr.to_state,
        version=tr.version,
    )


__all__ = ["router"]
