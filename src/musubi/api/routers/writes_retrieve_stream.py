"""NDJSON streaming variant of POST /v1/retrieve.

Per [[07-interfaces/canonical-api]] § NDJSON streaming, this endpoint
emits one JSON object per line so adapters can render results as they
arrive instead of waiting for the full batch. Useful for large
``limit`` queries.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import StreamingResponse

from musubi.api.dependencies import get_episodic_plane, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.routers.retrieve import RetrieveQuery
from musubi.auth import AuthRequirement, authenticate_request
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.types.common import Err

router = APIRouter(prefix="/v1/retrieve", tags=["retrieve-stream"])


@router.post(
    "/stream",
    operation_id="retrieve_stream.bucket=default",
)
async def retrieve_stream(
    request: Request,
    body: RetrieveQuery = Body(...),
    settings: Settings = Depends(get_settings_dep),
    episodic: EpisodicPlane = Depends(get_episodic_plane),
) -> StreamingResponse:
    requirement = AuthRequirement(namespace=body.namespace, access="r")
    result = authenticate_request(
        request,  # type: ignore[arg-type]
        requirement,
        settings=settings,
    )
    if isinstance(result, Err):
        err = result.error
        code: ErrorCode = err.code  # type: ignore[assignment]
        raise APIError(status_code=err.status_code, code=code, detail=err.detail)

    async def _emit() -> AsyncIterator[bytes]:
        rows = await episodic.query(
            namespace=body.namespace, query=body.query_text, limit=body.limit
        )
        for mem in rows:
            row = {
                "object_id": mem.object_id,
                "score": 1.0,
                "plane": "episodic",
                "content": mem.content,
                "namespace": mem.namespace,
            }
            yield (json.dumps(row) + "\n").encode("utf-8")

    return StreamingResponse(_emit(), media_type="application/x-ndjson")


__all__ = ["router"]
