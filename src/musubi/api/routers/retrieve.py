"""Retrieval read endpoint.

POST /v1/retrieve is a read in disguise — body carries the query
parameters; no state mutation. NDJSON streaming variant
(POST /v1/retrieve/stream) lives in ``writes_retrieve_stream.py``.

Dispatches to :func:`musubi.retrieve.orchestration.retrieve`, which
runs the per-mode pipeline (``fast`` → vector + recency + reinforcement
scoring; ``deep`` → full hybrid + cross-encoder rerank + lineage
hydration; ``blended`` → hybrid without the reranker). The router
itself does auth + body validation + error mapping; everything
interesting happens behind the orchestration boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel
from qdrant_client import QdrantClient

from musubi.api.dependencies import (
    get_embedder,
    get_qdrant_client,
    get_reranker,
    get_settings_dep,
)
from musubi.api.errors import APIError, ErrorCode
from musubi.api.responses import RetrieveResponse, RetrieveResultRow
from musubi.auth import AuthRequirement, authenticate_request
from musubi.embedding import Embedder, TEIRerankerClient
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
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


# orchestration.RetrievalError.kind → (HTTP status, typed error code).
# `timeout` maps to BACKEND_UNAVAILABLE because a timeout in orchestration
# means Qdrant or TEI didn't respond within budget — same shape as any
# upstream outage from the caller's perspective.
_KIND_STATUS_MAP: dict[str, tuple[int, ErrorCode]] = {
    "bad_query": (400, "BAD_REQUEST"),
    "forbidden": (403, "FORBIDDEN"),
    "timeout": (503, "BACKEND_UNAVAILABLE"),
    "internal": (500, "INTERNAL"),
}


@router.post("", response_model=RetrieveResponse)
async def retrieve(
    request: Request,
    body: RetrieveQuery = Body(...),
    settings: Settings = Depends(get_settings_dep),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    embedder: Embedder = Depends(get_embedder),
    reranker: TEIRerankerClient = Depends(get_reranker),
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

    # Build the orchestration query dict. Defaulting `planes` server-side
    # matches the pre-wiring router behaviour (single-plane episodic by
    # default) so callers that didn't know about cross-plane retrieval
    # don't suddenly start pulling from curated/concept too.
    query_body: dict[str, object] = {
        "namespace": body.namespace,
        "query_text": body.query_text,
        "mode": body.mode,
        "limit": body.limit,
        "planes": body.planes or ["episodic"],
        "include_archived": body.include_archived,
    }

    orchestration_result = await run_orchestration_retrieve(
        client=qdrant,
        embedder=embedder,
        reranker=reranker,
        query=query_body,
    )

    if isinstance(orchestration_result, Err):
        retrieval_err = orchestration_result.error
        status, error_code = _KIND_STATUS_MAP.get(retrieval_err.kind, (500, "INTERNAL"))
        raise APIError(status_code=status, code=error_code, detail=retrieval_err.detail)

    rows: list[RetrieveResultRow] = []
    for hit in orchestration_result.value:
        rows.append(
            RetrieveResultRow(
                object_id=hit.object_id,
                score=hit.score,
                plane=hit.plane,
                content=hit.snippet,
                namespace=hit.namespace,
                # Rich context stays in `extra` so the top-level response
                # shape (RetrieveResultRow) doesn't break for callers that
                # only want object_id / score / content.
                extra={
                    "score_components": hit.score_components,
                    "lineage": hit.lineage,
                    "title": hit.title,
                },
            )
        )

    return RetrieveResponse(
        results=rows[: body.limit],
        mode=body.mode,
        limit=body.limit,
    )


__all__ = ["router"]
