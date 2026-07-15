"""Concept write endpoints — reinforce / promote / reject / delete."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_concept_plane, get_lifecycle_service, get_settings_dep
from musubi.api.errors import APIError
from musubi.api.lifecycle_responses import TransitionPendingBody, pending_response
from musubi.config import Settings
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionPending
from musubi.planes.concept import ConceptPlane
from musubi.types.common import generate_ksuid, utc_now
from musubi.types.concept import SynthesizedConcept

router = APIRouter(prefix="/v1/concepts", tags=["concept-writes"])


class ReinforceRequest(BaseModel):
    additional_source: str | None = None


class PromoteRequest(BaseModel):
    promoted_to: str
    reason: str = "operator-force"


class RejectRequest(BaseModel):
    reason: str


@router.post(
    "/{object_id}/reinforce",
    response_model=SynthesizedConcept,
    operation_id="reinforce_concept.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def reinforce_concept(
    object_id: str,
    namespace: str = Query(...),
    body: ReinforceRequest = Body(default_factory=ReinforceRequest),
    plane: ConceptPlane = Depends(get_concept_plane),
) -> SynthesizedConcept:
    try:
        return await plane.reinforce(
            namespace=namespace,
            object_id=object_id,
            additional_source=body.additional_source or generate_ksuid(),
        )
    except LookupError as exc:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=str(exc),
        ) from exc


@router.post(
    "/{object_id}/promote",
    response_model=SynthesizedConcept,
    responses={202: {"model": TransitionPendingBody, "description": "Transition durably pending."}},
    operation_id="promote_concept.bucket=transition",
)
async def promote_concept(
    request: Request,
    object_id: str,
    namespace: str = Query(...),
    body: PromoteRequest = Body(...),
    plane: ConceptPlane = Depends(get_concept_plane),
    coordinator: LifecycleTransitionCoordinator = Depends(get_lifecycle_service),
    settings: Settings = Depends(get_settings_dep),
) -> SynthesizedConcept | Response:
    """Operator-forced promotion. Writes the matured→promoted transition
    on the concept with the supplied ``promoted_to`` curated id.

    Requires operator scope (read from the auth context attached by the
    inline ``require_auth`` invocation)."""
    # Inline auth: promote is operator-only.
    from musubi.api.auth import require_auth as _require

    # Invoke the dependency inline so we get the typed 401/403.
    # (We can't attach ``dependencies=[]`` at the decorator level with
    # operator=True here because the request has a required body.)
    dep = _require(operator=True)
    dep(request, settings)

    try:
        result = await plane.transition(
            namespace=namespace,
            object_id=object_id,
            to_state="promoted",
            actor="operator-api",
            reason=body.reason,
            promoted_to=body.promoted_to,
            promoted_at=utc_now(),
            coordinator=coordinator,
        )
    except LookupError as exc:
        raise APIError(status_code=404, code="NOT_FOUND", detail=str(exc)) from exc
    except ValueError as exc:
        raise APIError(status_code=400, code="BAD_REQUEST", detail=str(exc)) from exc
    if result.kind == "err":
        not_found = result.error.code == "not_found"
        raise APIError(
            status_code=404 if not_found else 400,
            code="NOT_FOUND" if not_found else "BAD_REQUEST",
            detail=result.error.message,
        )
    outcome = result.value
    if isinstance(outcome, TransitionPending):
        return pending_response(outcome)
    updated = await plane.get(namespace=namespace, object_id=object_id)
    if updated is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"concept {object_id!r} missing after finalized transition",
        )
    return updated


@router.post(
    "/{object_id}/reject",
    response_model=SynthesizedConcept,
    operation_id="reject_concept.bucket=transition",
)
async def reject_concept(
    request: Request,
    object_id: str,
    namespace: str = Query(...),
    body: RejectRequest = Body(...),
    plane: ConceptPlane = Depends(get_concept_plane),
) -> SynthesizedConcept:
    from musubi.api.auth import require_auth as _require
    from musubi.api.dependencies import get_settings_dep

    dep = _require(operator=True)
    dep(request, get_settings_dep())

    try:
        return await plane.record_promotion_rejection(
            namespace=namespace,
            object_id=object_id,
            reason=body.reason,
        )
    except LookupError as exc:
        raise APIError(status_code=404, code="NOT_FOUND", detail=str(exc)) from exc
    except ValueError as exc:
        raise APIError(status_code=400, code="BAD_REQUEST", detail=str(exc)) from exc


@router.delete(
    "/{object_id}",
    operation_id="delete_concept.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
    responses={202: {"model": TransitionPendingBody, "description": "Transition durably pending."}},
)
async def delete_concept(
    object_id: str,
    namespace: str = Query(...),
    plane: ConceptPlane = Depends(get_concept_plane),
    coordinator: LifecycleTransitionCoordinator = Depends(get_lifecycle_service),
) -> Response:
    # exists(), not get(): the transition below goes by object_id and never uses the
    # deserialized row, so a corrupted payload must not be able to block removal.
    # The removability of a memory must never depend on that memory being valid.
    if not await plane.exists(namespace=namespace, object_id=object_id):
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"concept {object_id!r} not found in namespace {namespace!r}",
        )
    # Concept state machine doesn't include "archived" directly — use
    # superseded as the soft-delete target (spec leaves this to the
    # caller; we pick the closest terminal state).
    try:
        result = await plane.transition(
            namespace=namespace,
            object_id=object_id,
            to_state="superseded",
            actor="api-delete",
            reason="api-soft-delete",
            coordinator=coordinator,
        )
    except ValueError as exc:
        raise APIError(status_code=400, code="BAD_REQUEST", detail=str(exc)) from exc
    if result.kind == "err":
        not_found = result.error.code == "not_found"
        raise APIError(
            status_code=404 if not_found else 400,
            code="NOT_FOUND" if not_found else "BAD_REQUEST",
            detail=result.error.message,
        )
    outcome = result.value
    if isinstance(outcome, TransitionPending):
        return pending_response(outcome)
    return Response(
        status_code=200, content=b'{"status":"superseded"}', media_type="application/json"
    )


__all__ = ["router"]
