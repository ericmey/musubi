"""Thought read endpoints — check (unread) + history (semantic search).

Both are POST endpoints per [[07-interfaces/canonical-api]] §5 (the body
carries query parameters). They're reads in disguise — no state mutation
— and live on the read surface per the slice-api-v0 split.

These endpoints take ``namespace`` in the request body, not the query
string, so they validate scope manually after body parse rather than
through the query-param-based ``require_auth`` dependency.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from pydantic import BaseModel
from qdrant_client import QdrantClient
from sse_starlette.sse import EventSourceResponse

from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.events import broker
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










async def _thoughts_event_generator(
    request: Request,
    namespace: str,
    includes: set[str],
    last_event_id: str | None,
    qdrant: QdrantClient,
    sub: Any,
) -> AsyncGenerator[dict[str, Any], None]:
    from musubi.types.common import utc_now

    async def _ping_loop() -> None:
        while True:
            await asyncio.sleep(0.01 if getattr(request.app.state, "testing", False) else 30.0)
            yield {
                "event": "ping",
                "data": json.dumps({"at": utc_now().isoformat()})
            }

    async def _sub_loop() -> None:
        while True:
            thought = await sub.queue.get()
            yield {
                "event": "thought",
                "id": str(thought.object_id),
                "data": thought.model_dump_json()
            }

    async def _check_disconnect() -> None:
        while True:
            if await request.is_disconnected():
                raise asyncio.CancelledError()
            await asyncio.sleep(0.5)

    # Wait, creating 3 generators and merging them is hard.
    # The SIMPLEST way to avoid `queue.get()` hanging without a timeout is:

    ping_interval = 0.01 if getattr(request.app.state, "testing", False) else 30.0

    try:
        while True:
            if await request.is_disconnected():
                break

            try:
                thought = await asyncio.wait_for(sub.queue.get(), timeout=ping_interval)
                yield {
                    "event": "thought",
                    "id": str(thought.object_id),
                    "data": thought.model_dump_json()
                }
            except TimeoutError:
                yield {
                    "event": "ping",
                    "data": json.dumps({"at": utc_now().isoformat()})
                }

    finally:
        broker.unsubscribe(sub)
        yield {
            "event": "close",
            "data": json.dumps({"reason": "server-shutdown", "reconnect_after_ms": 5000})
        }

@router.get(
    "/stream",
    operation_id="stream_thoughts.bucket=default",
)
async def stream_thoughts(
    request: Request,
    namespace: str = Query(...),
    include: str | None = Query(None),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> Any:
    # Authenticate manually since namespace is query param
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

    ctx = result.value

    includes = set(include.split(",")) if include else {ctx.presence, "all"}

    try:
        sub = broker.subscribe(namespace, includes)
    except ConnectionError:
        from musubi.api.errors import error_response

        # We need to return a JSONResponse with the header
        # error_response doesn't take headers, so we construct it
        resp = error_response(status_code=503, detail="Connection cap exceeded", code="BACKEND_UNAVAILABLE")
        resp.headers["Retry-After"] = "5"
        return resp

    return EventSourceResponse(_thoughts_event_generator(request, namespace, includes, last_event_id, qdrant, sub))

__all__ = ["router"]
