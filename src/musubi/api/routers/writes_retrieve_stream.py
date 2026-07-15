"""NDJSON streaming variant of POST /v1/retrieve.

Per [[07-interfaces/canonical-api]] § NDJSON streaming, this endpoint
emits one JSON object per line. Note that the envelope is fully materialized
by orchestration BEFORE serialization begins — this endpoint provides wire
streaming parity (NDJSON shape and headers) for client parsers, but does
NOT reduce incremental backend retrieval latency (all hits are fetched before
the stream starts).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import StreamingResponse
from qdrant_client import QdrantClient

from musubi.api.dependencies import (
    get_embedder,
    get_qdrant_client,
    get_reranker,
    get_settings_dep,
)
from musubi.api.errors import APIError, ErrorCode
from musubi.api.responses import (
    RankedExtra,
    RankedResultRow,
    RankedScoreComponents,
    RecentExtra,
    RecentResultRow,
    RecentScoreComponents,
)
from musubi.api.routers.retrieve import (
    _KIND_STATUS_MAP,
    RetrieveQuery,
    _expand_wildcard_targets,
    _resolve_targets,
)
from musubi.auth import authenticate_request
from musubi.auth.scopes import enforce_namespace_policy
from musubi.embedding import Embedder, TEIRerankerClient
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
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
    qdrant: QdrantClient = Depends(get_qdrant_client),
    embedder: Embedder = Depends(get_embedder),
    reranker: TEIRerankerClient = Depends(get_reranker),
) -> StreamingResponse:
    # AUTH-001: when the body omits ``namespace``, defer the target
    # resolution until after auth so we can derive the caller's
    # identity_family.
    if body.namespace is None:
        targets: list[tuple[str, str]] = []
        shape_err: str | None = None
    else:
        targets, shape_err = _resolve_targets(body.namespace, body.planes)
        if shape_err is not None:
            raise APIError(status_code=400, code="BAD_REQUEST", detail=shape_err)

    auth_result = authenticate_request(
        request,  # type: ignore[arg-type]
        None,
        settings=settings,
    )
    if isinstance(auth_result, Err):
        err = auth_result.error
        code: ErrorCode = err.code  # type: ignore[assignment]
        raise APIError(status_code=err.status_code, code=code, detail=err.detail)
    context = auth_result.value

    if body.namespace is None:
        from musubi.api.routers.retrieve import _enumerate_authorized_targets

        family = context.presence.split("/", 1)[0]
        planes = body.planes or ["curated", "concept", "episodic"]
        targets = _enumerate_authorized_targets(qdrant, family=family, planes=planes)

    pattern_had_wildcards = any("*" in ns for ns, _ in targets)
    targets = _expand_wildcard_targets(qdrant, targets)

    if pattern_had_wildcards and not targets:
        headers = {
            "X-Musubi-Mode": body.mode,
            "X-Musubi-Limit": str(body.limit),
            "X-Musubi-Warnings": "[]",
        }

        async def _emit_empty() -> AsyncIterator[bytes]:
            if False:
                yield b""

        return StreamingResponse(_emit_empty(), media_type="application/x-ndjson", headers=headers)

    # AUTH-001: the shared READ-ONLY enforcement seam.
    policy_result = enforce_namespace_policy(
        context,
        targets=targets,
        settings=settings,
        reject_unauthorized=body.namespace is not None,
    )
    if isinstance(policy_result, Err):
        raise APIError(
            status_code=policy_result.error.status_code,
            code="FORBIDDEN",
            detail=policy_result.error.detail,
        )
    targets = policy_result.value

    if not targets:
        headers = {
            "X-Musubi-Mode": body.mode,
            "X-Musubi-Limit": str(body.limit),
            "X-Musubi-Warnings": "[]",
        }

        async def _emit_empty() -> AsyncIterator[bytes]:
            if False:
                yield b""
            return

        return StreamingResponse(_emit_empty(), media_type="application/x-ndjson", headers=headers)

    query_body: dict[str, object] = {
        "namespace": body.namespace or "",
        "query_text": body.query_text,
        "mode": body.mode,
        "limit": body.limit,
        "planes": [plane for _, plane in targets],
        "include_archived": body.include_archived,
        "namespace_targets": [{"namespace": ns, "plane": plane} for ns, plane in targets],
    }
    if body.state_filter is not None:
        query_body["state_filter"] = body.state_filter
    if body.since is not None:
        query_body["since"] = body.since
    if body.tags is not None:
        query_body["tags"] = body.tags
    query_body["include_lineage"] = body.include_lineage

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

    envelope = orchestration_result.value
    warnings_json = json.dumps([warning.code for warning in envelope.warnings])

    headers = {
        "X-Musubi-Mode": body.mode,
        "X-Musubi-Limit": str(body.limit),
        "X-Musubi-Warnings": warnings_json,
    }

    async def _emit() -> AsyncIterator[bytes]:
        for hit in envelope.results[: body.limit]:
            if body.mode == "recent":
                recent_extra = RecentExtra(
                    score_components=RecentScoreComponents(),
                    lineage=hit.lineage,
                )
                row_model_recent = RecentResultRow(
                    object_id=hit.object_id,
                    score=hit.score,
                    plane=hit.plane,
                    content=hit.snippet,
                    namespace=hit.namespace,
                    title=hit.title,
                    state=hit.state,
                    importance=hit.importance,
                    score_kind="created_epoch",
                    provenance_score=hit.provenance_score,
                    extra=recent_extra,
                )
                yield (row_model_recent.model_dump_json(exclude_unset=True) + "\n").encode("utf-8")
            else:
                ranked_components = RankedScoreComponents(
                    relevance=hit.score_components["relevance"],
                    recency=hit.score_components["recency"],
                    importance=hit.score_components["importance"],
                    provenance=hit.score_components["provenance"],
                    reinforcement=hit.score_components["reinforcement"],
                )
                ranked_extra = RankedExtra(
                    score_components=ranked_components,
                    lineage=hit.lineage,
                )
                row_model_ranked = RankedResultRow(
                    object_id=hit.object_id,
                    score=hit.score,
                    plane=hit.plane,
                    content=hit.snippet,
                    namespace=hit.namespace,
                    title=hit.title,
                    state=hit.state,
                    importance=hit.importance,
                    score_kind="ranked_combined",
                    extra=ranked_extra,
                )
                yield (row_model_ranked.model_dump_json(exclude_unset=True) + "\n").encode("utf-8")

    return StreamingResponse(_emit(), media_type="application/x-ndjson", headers=headers)


__all__ = ["router"]
