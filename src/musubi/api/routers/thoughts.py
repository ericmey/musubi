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

from musubi.api.dependencies import get_qdrant_client, get_settings_dep, get_thoughts_plane
from musubi.api.errors import APIError, ErrorCode
from musubi.api.events import Subscription, broker
from musubi.api.responses import ThoughtListResponse
from musubi.api.routers._scroll import scroll_namespace
from musubi.auth import AuthRequirement, authenticate_request
from musubi.planes.thoughts import ThoughtsPlane
from musubi.settings import Settings
from musubi.types.common import Err, utc_now
from musubi.types.thought import Thought

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
    replay: list[Thought] | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield SSE byte frames until the client disconnects or the server shuts down.

    In test mode (``app.state.testing = True``), the ping cadence drops from 30s
    to 10ms so tests observe pings sub-second. Queue reads use
    ``asyncio.wait_for`` bounded by the ping interval so the loop always
    makes progress (delivers a ping on timeout, a thought otherwise).

    Replay — if ``replay`` is provided (from a ``Last-Event-ID`` reconnect),
    those frames emit first in strictly-ascending ``object_id`` order, then
    the generator transitions to live-tail from the broker queue. Because
    the broker subscription was opened BEFORE the replay query, any
    thought published during replay is captured in the queue and emitted
    as part of live-tail — no gap.

    Cancellation path: when the HTTP connection is closed client-side,
    Starlette cancels this coroutine; the ``finally`` block unsubscribes.
    """
    ping_interval = 0.01 if getattr(request.app.state, "testing", False) else 30.0

    try:
        if replay:
            for t in replay:
                yield _sse_frame(
                    event="thought",
                    event_id=str(t.object_id),
                    data=t.model_dump_json(),
                )

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


@router.get(
    "/stream",
    operation_id="stream_thoughts.bucket=default",
    responses={
        200: {
            "description": "Server-sent event stream of delivered thoughts.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        403: {"description": "Caller does not hold read scope for `namespace`."},
        503: {"description": "Thought broker unavailable."},
    },
)
async def stream_thoughts(
    request: Request,
    namespace: str = Query(...),
    include: str | None = Query(None),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
    thoughts_plane: ThoughtsPlane = Depends(get_thoughts_plane),
) -> Any:
    """SSE endpoint for real-time thought delivery.

    Replay: if ``Last-Event-ID: <ksuid>`` is present, every thought matching
    the subscription scope where ``object_id > last_event_id`` (lex,
    ascending) is emitted before entering live-tail mode. Capped at 500
    events — if more matched, the ``X-Musubi-Replay-Truncated: true``
    response header tells the client to fall back to ``/v1/thoughts/history``
    for deeper backfill.
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
        # Subscribe BEFORE replay query so any thought published during
        # the replay fetch lands in the live-tail queue — no gap.
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

    replay: list[Thought] = []
    truncated = False
    if last_event_id:
        replay, truncated = await thoughts_plane.replay_since(
            namespace=namespace,
            includes=includes,
            last_event_id=last_event_id,
        )

    response_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
    }
    if truncated:
        # Client uses this signal to backfill via POST /v1/thoughts/history
        # for the span the replay cap couldn't cover.
        response_headers["X-Musubi-Replay-Truncated"] = "true"

    return StreamingResponse(
        _thoughts_event_generator(request, sub, replay=replay),
        media_type="text/event-stream",
        headers=response_headers,
    )


__all__ = ["router"]
