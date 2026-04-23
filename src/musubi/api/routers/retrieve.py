"""Retrieval read endpoint.

POST /v1/retrieve is a read in disguise — body carries the query
parameters; no state mutation. NDJSON streaming variant
(POST /v1/retrieve/stream) lives in ``writes_retrieve_stream.py``.

Two namespace shapes are accepted:

- **3-segment** (``tenant/presence/plane``): single-plane query. The
  stored-row filter is literal; the ``planes`` field, if set, must
  not contradict the namespace's trailing plane.
- **2-segment** (``tenant/presence``): cross-plane query. Each entry
  in ``planes`` is expanded to ``<namespace>/<plane>`` server-side
  and the pipeline fans out, merging results by score. Scope is
  checked **strictly per plane** — a token requesting any plane it
  can't read 403s the entire request rather than silently omitting
  that plane (ADR 0028).

Dispatches to :func:`musubi.retrieve.orchestration.retrieve`, which
runs the per-mode pipeline (``fast`` → vector + recency + reinforcement
scoring; ``deep`` → full hybrid + cross-encoder rerank + lineage
hydration; ``blended`` → hybrid without the reranker). The router
does auth + body validation + shape expansion + error mapping;
everything interesting happens behind the orchestration boundary.
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
from musubi.auth import authenticate_request
from musubi.auth.scopes import resolve_namespace_scope
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


_VALID_PLANES: frozenset[str] = frozenset({"episodic", "curated", "concept", "artifact"})


def _namespace_shape(namespace: str) -> int:
    """Number of ``/``-separated segments in ``namespace``."""
    return len(namespace.split("/"))


def _resolve_targets(
    namespace: str,
    planes: list[str] | None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Expand a retrieve body into concrete (namespace, plane) targets.

    Returns ``(targets, error)``. ``error`` is a string describing a
    shape problem (unknown plane, 3-seg/planes mismatch); targets is
    empty in that case. A valid expansion always produces at least
    one target.

    - 3-segment namespace: single target. If ``planes`` is set, it
      must be a single-element list matching the namespace's trailing
      segment; otherwise we're silently discarding whatever the
      caller asked for.
    - 2-segment namespace: one target per entry in ``planes``. If
      ``planes`` is unset, default to ``["episodic"]`` to match the
      pre-fanout behaviour.
    """
    shape = _namespace_shape(namespace)
    requested = list(planes) if planes else None

    # Reject empty segments up front so `a/b/` doesn't slip through
    # as a "3-segment" with trailing empty plane.
    if any(seg == "" for seg in namespace.split("/")):
        return ([], f"namespace '{namespace}' has empty segments")

    if shape == 3:
        derived_plane = namespace.rsplit("/", 1)[-1]
        if derived_plane not in _VALID_PLANES:
            return (
                [],
                f"3-segment namespace '{namespace}' names unknown plane "
                f"'{derived_plane}' (valid: {sorted(_VALID_PLANES)})",
            )
        if requested is not None and requested != [derived_plane]:
            return (
                [],
                f"3-segment namespace '{namespace}' pins plane "
                f"'{derived_plane}'; planes={requested} is inconsistent",
            )
        return ([(namespace, derived_plane)], None)

    if shape == 2:
        target_planes = requested if requested is not None else ["episodic"]
        # Dedup in-order: `planes=["episodic", "episodic"]` is either
        # a typo or a retry shape; either way running the pipeline
        # twice for the same target wastes work and can skew merge
        # ordering. Keep first-seen order so the caller's intent is
        # preserved.
        seen: set[str] = set()
        deduped: list[str] = []
        for plane in target_planes:
            if plane in seen:
                continue
            seen.add(plane)
            deduped.append(plane)
        for plane in deduped:
            if plane not in _VALID_PLANES:
                return (
                    [],
                    f"unknown plane '{plane}' in planes list (valid: {sorted(_VALID_PLANES)})",
                )
        return ([(f"{namespace}/{plane}", plane) for plane in deduped], None)

    return ([], f"namespace '{namespace}' must be 2- or 3-segment")


@router.post("", response_model=RetrieveResponse)
async def retrieve(
    request: Request,
    body: RetrieveQuery = Body(...),
    settings: Settings = Depends(get_settings_dep),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    embedder: Embedder = Depends(get_embedder),
    reranker: TEIRerankerClient = Depends(get_reranker),
) -> RetrieveResponse:
    targets, shape_err = _resolve_targets(body.namespace, body.planes)
    if shape_err is not None:
        raise APIError(status_code=400, code="BAD_REQUEST", detail=shape_err)

    # Authenticate once — re-verifying the JWT per target would be
    # O(#planes) token validation overhead for no gain. Then resolve
    # the scope check strictly against every target from the single
    # context; first denial aborts the whole request per ADR 0028.
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

    for target_namespace, _plane in targets:
        scope_result = resolve_namespace_scope(context, namespace=target_namespace, access="r")
        if isinstance(scope_result, Err):
            raise APIError(
                status_code=scope_result.error.status_code,
                code="FORBIDDEN",
                detail=scope_result.error.detail,
            )

    # Hand orchestration the fully-resolved targets. A 3-segment
    # call reduces to exactly one (namespace, plane) target, so the
    # single-plane code path is preserved bit-for-bit.
    query_body: dict[str, object] = {
        "namespace": body.namespace,
        "query_text": body.query_text,
        "mode": body.mode,
        "limit": body.limit,
        "planes": [plane for _, plane in targets],
        "include_archived": body.include_archived,
        "namespace_targets": [{"namespace": ns, "plane": plane} for ns, plane in targets],
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
