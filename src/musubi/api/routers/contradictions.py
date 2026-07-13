"""Contradictions list endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth, require_operator_scope
from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError
from musubi.api.responses import ContradictionListResponse, ContradictionPair
from musubi.settings import Settings

router = APIRouter(prefix="/v1/contradictions", tags=["contradictions"])


@router.get(
    "",
    response_model=ContradictionListResponse,
    dependencies=[Depends(require_auth())],
)
async def list_contradictions(
    request: Request,
    namespace: str | None = Query(None),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    settings: Settings = Depends(get_settings_dep),
) -> ContradictionListResponse:
    """Concept rows whose ``contradicts`` field is non-empty.

    Namespace-scoped when ``namespace`` is supplied (the route-level ``require_auth`` authorizes
    the query namespace). Omitting the namespace is a cross-tenant fan-out and requires OPERATOR
    scope (SEC-004) — never an all-tenant scroll under ordinary auth. The data source is the
    concept plane payload — concepts mark each other as contradicting during synthesis
    (slice-lifecycle-synthesis).
    """
    scroll_filter: models.Filter | None = None
    if namespace is not None:
        scroll_filter = models.Filter(
            must=[
                models.FieldCondition(key="namespace", match=models.MatchValue(value=namespace)),
            ]
        )
    else:
        # SEC-004: an omitted namespace scrolls every tenant's concepts. That fan-out must
        # require operator scope, not the ordinary require_auth on the route.
        require_operator_scope(request, settings=settings)
    try:
        records, _ = qdrant.scroll(
            collection_name="musubi_concept",
            scroll_filter=scroll_filter,
            limit=200,
            with_payload=True,
        )
    except Exception as exc:
        # SEC-004 / RET-007: a backend outage must surface as an error, never a clean-looking
        # empty 200 that is indistinguishable from "no contradictions".
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="contradictions backend unavailable",
        ) from exc

    pairs: list[ContradictionPair] = []
    for rec in records:
        if not rec.payload:
            continue
        contradicts = rec.payload.get("contradicts") or []
        if not contradicts:
            continue
        pairs.append(
            ContradictionPair(
                object_id=str(rec.payload.get("object_id", "")),
                contradicts=[str(c) for c in contradicts],
                namespace=str(rec.payload.get("namespace", "")),
            )
        )
    return ContradictionListResponse(items=pairs)


__all__ = ["router"]
