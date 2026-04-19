"""Retrieval read endpoint.

POST /v1/retrieve is a read in disguise — body carries the query
parameters; no state mutation. NDJSON streaming variant
(POST /v1/retrieve/stream) is deferred to slice-api-v0-write.

The retrieval implementation lives in ``src/musubi/retrieve/`` and is
owned by the retrieval slices. This router does the minimum: parses the
query body, gates scope, and routes to either the per-plane ``query``
methods (first cut) or to the future blended retriever once
slice-retrieval-orchestration wires it through. The single-plane fast
path uses ``EpisodicPlane.query`` today; cross-plane blended retrieval
arrives via slice-retrieval-blended.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel

from musubi.api.dependencies import (
    get_concept_plane,
    get_curated_plane,
    get_episodic_plane,
    get_settings_dep,
)
from musubi.api.errors import APIError, ErrorCode
from musubi.api.responses import RetrieveResponse, RetrieveResultRow
from musubi.auth import AuthRequirement, authenticate_request
from musubi.planes.concept import ConceptPlane
from musubi.planes.curated import CuratedPlane
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.types.common import Err

router = APIRouter(prefix="/v1/retrieve", tags=["retrieve"])


class RetrieveQuery(BaseModel):
    namespace: str
    query_text: str
    mode: str = "fast"
    limit: int = 10
    planes: list[str] | None = None
    include_archived: bool = False


@router.post("", response_model=RetrieveResponse)
async def retrieve(
    request: Request,
    body: RetrieveQuery = Body(...),
    settings: Settings = Depends(get_settings_dep),
    episodic: EpisodicPlane = Depends(get_episodic_plane),
    curated: CuratedPlane = Depends(get_curated_plane),
    concept: ConceptPlane = Depends(get_concept_plane),
) -> RetrieveResponse:
    requirement = AuthRequirement(namespace=body.namespace, access="r")
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

    requested_planes = set(body.planes or ["episodic"])
    rows: list[RetrieveResultRow] = []

    # First cut: dispatch to each requested plane's query() in turn and
    # interleave the results. Cross-plane scoring/dedup lives in
    # slice-retrieval-orchestration; this router carries the API surface.
    plane_namespace_map = {
        "episodic": ("episodic", body.namespace),
        "curated": ("curated", body.namespace.rsplit("/", 1)[0] + "/curated"),
        "concept": ("concept", body.namespace.rsplit("/", 1)[0] + "/concept"),
    }

    if "episodic" in requested_planes:
        for mem in await episodic.query(
            namespace=body.namespace, query=body.query_text, limit=body.limit
        ):
            rows.append(
                RetrieveResultRow(
                    object_id=mem.object_id,
                    score=1.0,
                    plane="episodic",
                    content=mem.content,
                    namespace=mem.namespace,
                )
            )
    if "curated" in requested_planes:
        for cur in await curated.query(
            namespace=plane_namespace_map["curated"][1],
            query=body.query_text,
            limit=body.limit,
        ):
            rows.append(
                RetrieveResultRow(
                    object_id=cur.object_id,
                    score=1.0,
                    plane="curated",
                    content=cur.content,
                    namespace=cur.namespace,
                )
            )
    if "concept" in requested_planes:
        for con in await concept.query(
            namespace=plane_namespace_map["concept"][1],
            query=body.query_text,
            limit=body.limit,
        ):
            rows.append(
                RetrieveResultRow(
                    object_id=con.object_id,
                    score=1.0,
                    plane="concept",
                    content=con.content,
                    namespace=con.namespace,
                )
            )

    return RetrieveResponse(
        results=rows[: body.limit],
        mode=body.mode,
        limit=body.limit,
    )


__all__ = ["router"]
