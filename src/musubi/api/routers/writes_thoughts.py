"""Thought write endpoints — send + read."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.auth import AuthRequirement, authenticate_request
from musubi.embedding.fake import FakeEmbedder
from musubi.planes.thoughts import ThoughtsPlane
from musubi.settings import Settings
from musubi.types.common import Err
from musubi.types.thought import Thought

router = APIRouter(prefix="/v1/thoughts", tags=["thoughts-writes"])


class ThoughtSendRequest(BaseModel):
    namespace: str
    from_presence: str = Field(min_length=1)
    to_presence: str = Field(min_length=1)
    content: str = Field(min_length=1)
    channel: str = "default"
    importance: int = Field(default=5, ge=1, le=10)


class ThoughtSendResponse(BaseModel):
    object_id: str
    state: str


class ThoughtReadRequest(BaseModel):
    namespace: str
    ids: list[str]
    reader: str = Field(min_length=1)


class ThoughtReadResponse(BaseModel):
    count: int


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


@router.post(
    "/send",
    response_model=ThoughtSendResponse,
    status_code=202,
    operation_id="send_thought.bucket=thought",
)
async def send_thought(
    request: Request,
    body: ThoughtSendRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> ThoughtSendResponse:
    _check_body_scope(request, body.namespace, settings)
    plane = ThoughtsPlane(client=qdrant, embedder=FakeEmbedder())
    thought = Thought(
        namespace=body.namespace,
        from_presence=body.from_presence,
        to_presence=body.to_presence,
        content=body.content,
        channel=body.channel,
        importance=body.importance,
    )
    saved = await plane.send(thought)

    # Fire-and-forget publish hook
    from musubi.api.events import broker

    broker.publish(saved)

    return ThoughtSendResponse(object_id=saved.object_id, state=saved.state)


@router.post(
    "/read",
    response_model=ThoughtReadResponse,
    operation_id="read_thoughts.bucket=default",
)
async def read_thoughts(
    request: Request,
    body: ThoughtReadRequest = Body(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> ThoughtReadResponse:
    _check_body_scope(request, body.namespace, settings)
    plane = ThoughtsPlane(client=qdrant, embedder=FakeEmbedder())
    count = 0
    for oid in body.ids:
        try:
            await plane.read(namespace=body.namespace, object_id=oid, reader=body.reader)
            count += 1
        except (LookupError, ValueError):
            continue
    return ThoughtReadResponse(count=count)


__all__ = ["router"]
