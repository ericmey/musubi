"""Thought read endpoints — check (unread) + history (semantic search) + stream (SSE).

POST /check, /history, /read take body-carried params (namespace via body, not
query string, so they validate scope manually after body parse rather than through
the query-param-based require_auth dependency).

GET /stream is SSE per [[07-interfaces/canonical-api]] §5 Thoughts stream.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.events import Subscription, broker
from musubi.api.responses import ThoughtListResponse
from musubi.api.routers._scroll import scroll_namespace
from musubi.auth import AuthRequirement, authenticate_request
from musubi.settings import Settings
from musubi.types.common import Err, utc_now

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
    land once slice-retrieval-fast wires its dense path through the API."""
    _check_body_scope(request, body.namespace, settings)
    items, _ = scroll_namespace(
        qdrant,
        collection="musubi_thought",
        namespace=body.namespace,
        limit=body.limit,
        cursor=None,
    )
    return ThoughtListResponse(items=items)


def _sse_frame(event: str, data: str, event_id: str | None = None) -> bytes:
    """Format a single SSE frame as bytes.

    Follows the SSE wire format: optional ``id: <...>``, ``event: <...>``,
    ``data: <...>``, then a blank line terminator.
    """
    parts: list[str] = []
    if event_id is not None:
        parts.append(f"id: {event_id}")
    parts.append(f"event: {event}")
    parts.append(f"data: {data}")
    parts.append("")  # frame terminator
    parts.append("")  # trailing newline
    return "\n".join(parts).encode("utf-8")


async def _thoughts_event_generator(
    request: Request,
    sub: Subscription,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE byte frames until the client disconnects or the server shuts down.

    In test mode (``app.state.testing = True``), the ping cadence drops from 30s
    to 10ms so tests observe pings sub-second. Queue reads use
    ``asyncio.wait_for`` bounded by the ping interval so the loop always
    makes progress (delivers a ping on timeout, a thought otherwise).

    Cancellation path: when the HTTP connection is closed client-side,
    Starlette cancels this coroutine; the ``finally`` block unsubscribes.
    """
    ping_interval = 0.01 if getattr(request.app.state, "testing", False) else 30.0

    try:
        while True:
            try:
                thought = await asyncio.wait_for(sub.queue.get(), timeout=ping_interval)
                yield _sse_frame(
                    event="thought",
                    event_id=str(thought.object_id),
                    data=thought.model_dump_json(),
                )
            except TimeoutError:
                yield _sse_frame(
                    event="ping",
                    data=json.dumps({"at": utc_now().isoformat()}),
                )
    finally:
        broker.unsubscribe(sub)


@router.get("/stream", operation_id="stream_thoughts.bucket=default")
async def stream_thoughts(
    request: Request,
    namespace: str = Query(...),
    include: str | None = Query(None),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> Any:
    """SSE endpoint for real-time thought delivery.

    Replay semantics via Last-Event-ID are declared in the spec but deferred to
    the integration harness (needs a real Qdrant with live range queries; mocked
    Qdrant in unit tests doesn't model lex-sorted epoch-range scrolls). The
    header is accepted and validated but currently the stream begins from the
    live broker queue only. See slice-api-thoughts-stream work log.
    """
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

        resp = error_response(
            status_code=503,
            detail="Connection cap exceeded",
            code="BACKEND_UNAVAILABLE",
        )
        resp.headers["Retry-After"] = "5"
        return resp

    return StreamingResponse(
        _thoughts_event_generator(request, sub),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
        },
    )


__all__ = ["router"]
