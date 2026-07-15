"""Context-pack endpoint for essence alignment."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field, model_validator
from qdrant_client import QdrantClient

from musubi.api.dependencies import get_embedder, get_qdrant_client, get_reranker, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.routers.retrieve import _KIND_STATUS_MAP, _expand_wildcard_targets, _resolve_targets
from musubi.auth import authenticate_request
from musubi.auth.scopes import resolve_namespace_scope
from musubi.embedding import Embedder, TEIRerankerClient
from musubi.retrieve.accounting import account_delivered
from musubi.retrieve.context_pack import (
    ContextCandidate,
    ContextPack,
    ContextPackQuery,
    build_context_pack,
)
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.settings import Settings
from musubi.types.common import Err

router = APIRouter(prefix="/v1/context", tags=["context"])


class ContextQuery(BaseModel):
    """Build a small, grouped context pack for a presence."""

    namespace: str = Field(
        ...,
        description="Two- or three-segment namespace, same shape as /v1/retrieve.",
    )
    query_text: str = Field(min_length=1)
    mode: Literal["startup"] = "startup"
    planes: list[str] | None = Field(default_factory=lambda: ["episodic", "curated", "concept"])
    candidate_limit: int = Field(default=30, ge=1, le=100)
    max_items: int = Field(default=8, ge=1, le=50)
    max_chars: int = Field(default=1200, ge=120, le=8000)
    include_history: bool = False
    state_filter: list[str] | None = None

    @model_validator(mode="after")
    def _state_filter_for_history(self) -> ContextQuery:
        if self.include_history and self.state_filter is None:
            self.state_filter = [
                "provisional",
                "matured",
                "promoted",
                "demoted",
                "archived",
                "superseded",
            ]
        return self


@router.post("", response_model=ContextPack)
async def context_pack(
    request: Request,
    body: ContextQuery = Body(...),
    settings: Settings = Depends(get_settings_dep),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    embedder: Embedder = Depends(get_embedder),
    reranker: TEIRerankerClient = Depends(get_reranker),
) -> ContextPack:
    """Return a ranked, grouped context pack.

    Ranking happens server-side because clients should not have to
    rehydrate payload metadata or maintain their own essence heuristics.
    """

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

    pattern_had_wildcards = any("*" in ns for ns, _ in targets)
    targets = _expand_wildcard_targets(qdrant, targets)
    if pattern_had_wildcards and not targets:
        return build_context_pack(
            [],
            ContextPackQuery(
                query_text=body.query_text,
                mode=body.mode,
                max_items=body.max_items,
                max_chars=body.max_chars,
                include_history=body.include_history,
            ),
        )

    for target_namespace, _plane in targets:
        scope_result = resolve_namespace_scope(context, namespace=target_namespace, access="r")
        if isinstance(scope_result, Err):
            raise APIError(
                status_code=scope_result.error.status_code,
                code="FORBIDDEN",
                detail=scope_result.error.detail,
            )

    query_body: dict[str, object] = {
        "namespace": body.namespace,
        "query_text": body.query_text,
        "mode": "fast",
        "limit": body.candidate_limit,
        "planes": [plane for _, plane in targets],
        "include_archived": body.include_history,
        "namespace_targets": [{"namespace": ns, "plane": plane} for ns, plane in targets],
        "state_filter": body.state_filter or ["provisional", "matured", "promoted"],
    }
    # RET-002 (#500): /v1/context's DELIVERED set is the final pack, not the retrieval
    # candidates — build_context_pack trims by max_items/max_chars/filler below. Defer access
    # accounting (account_access=False) and account the surfaced pack items ourselves, so a
    # trimmed candidate is never counted.
    orchestration_result = await run_orchestration_retrieve(
        client=qdrant,
        embedder=embedder,
        reranker=reranker,
        query=query_body,
        account_access=False,
    )
    if isinstance(orchestration_result, Err):
        retrieval_err = orchestration_result.error
        status, error_code = _KIND_STATUS_MAP.get(retrieval_err.kind, (500, "INTERNAL"))
        raise APIError(status_code=status, code=error_code, detail=retrieval_err.detail)

    envelope = orchestration_result.value
    candidates = [_candidate_from_hit(hit) for hit in envelope.results]
    # RET-007: thread the bounded degradation codes onto the pack so /v1/context is NOT a surface where
    # degraded context is indistinguishable from healthy.
    pack = build_context_pack(
        candidates,
        ContextPackQuery(
            query_text=body.query_text,
            mode=body.mode,
            max_items=body.max_items,
            max_chars=body.max_chars,
            include_history=body.include_history,
        ),
        warnings=[warning.code for warning in envelope.warnings],
    )
    # Account exactly the FINAL surfaced items (each carries namespace + object_id + plane), once.
    # An empty pack accounts nothing. Fail-loud but bounded: an accounting failure becomes an
    # INTERNAL APIError, never a raw exception leaked to the caller.
    try:
        await account_delivered(qdrant, [item for group in pack.groups for item in group.items])
    except Exception:
        raise APIError(
            status_code=500, code="INTERNAL", detail="access accounting failed"
        ) from None
    return pack


def _candidate_from_hit(hit: Any) -> ContextCandidate:
    payload = hit.payload if isinstance(hit.payload, dict) else {}
    return ContextCandidate(
        object_id=str(hit.object_id),
        namespace=str(payload.get("namespace") or hit.namespace),
        plane=str(payload.get("plane") or hit.plane),
        content=str(payload.get("content") or hit.snippet),
        summary=_optional_str(payload.get("summary")),
        title=_optional_str(payload.get("title") or hit.title),
        tags=_string_list(payload.get("tags")),
        state=str(payload.get("state") or "matured"),
        created_epoch=_optional_float(payload.get("created_epoch")) or 0.0,
        updated_epoch=_optional_float(payload.get("updated_epoch")),
        importance=int(payload.get("importance") or 5),
        retrieve_score=float(hit.score),
        extra={key: value for key, value in payload.items() if key in {"kind", "staleness"}},
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


__all__ = ["ContextQuery", "router"]
