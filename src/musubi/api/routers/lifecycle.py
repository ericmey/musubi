"""Lifecycle event read endpoints — listing the audit log."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from qdrant_client import QdrantClient

from musubi.api.auth import require_operator
from musubi.api.dependencies import get_qdrant_client
from musubi.api.responses import LifecycleEventListResponse, LifecycleEventRow
from musubi.api.routers._scroll import scroll_namespace

router = APIRouter(prefix="/v1/lifecycle", tags=["lifecycle"])


def _make_row(payload: dict[str, object]) -> LifecycleEventRow:
    raw_epoch = payload.get("occurred_epoch", 0.0)
    epoch = float(raw_epoch) if isinstance(raw_epoch, int | float) else 0.0
    return LifecycleEventRow(
        event_id=str(payload.get("event_id", "")),
        object_id=str(payload.get("object_id", "")),
        object_type=str(payload.get("object_type", "")),
        namespace=str(payload.get("namespace", "")),
        from_state=str(payload.get("from_state", "")),
        to_state=str(payload.get("to_state", "")),
        actor=str(payload.get("actor", "")),
        reason=str(payload.get("reason", "")),
        occurred_epoch=epoch,
    )


@router.get(
    "/events",
    response_model=LifecycleEventListResponse,
    dependencies=[Depends(require_operator())],
)
async def list_events(
    namespace: str | None = Query(None),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> LifecycleEventListResponse:
    """Operator-only: list lifecycle events.

    The events themselves are recorded in a sqlite ledger by the
    lifecycle engine (see ``src/musubi/lifecycle/events.py``); the
    Qdrant mirror collection ``musubi_lifecycle_events`` is the
    listable view per [[04-data-model/qdrant-layout]]. If the mirror
    isn't populated yet (the slice that does the dual-write hasn't
    landed), the list comes back empty rather than erroring.
    """
    if namespace is None:
        # Without a namespace filter we still need a stable scroll.
        # For now: empty list — the lifecycle-engine mirror that this
        # endpoint will read from is a follow-up.
        return LifecycleEventListResponse(items=[])
    items, _ = scroll_namespace(
        qdrant,
        collection="musubi_lifecycle_events",
        namespace=namespace,
        limit=200,
        cursor=None,
    )
    return LifecycleEventListResponse(items=[_make_row(p) for p in items])


@router.get(
    "/events/{object_id}",
    response_model=LifecycleEventListResponse,
    dependencies=[Depends(require_operator())],
)
async def list_events_for_object(
    object_id: str,
) -> LifecycleEventListResponse:
    return LifecycleEventListResponse(items=[])


__all__ = ["router"]
