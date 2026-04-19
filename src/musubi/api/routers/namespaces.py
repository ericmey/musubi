"""Namespaces list + per-namespace stats endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request
from qdrant_client import QdrantClient, models

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.api.responses import NamespaceListResponse, NamespaceStats
from musubi.auth import AuthRequirement, authenticate_request
from musubi.settings import Settings
from musubi.types.common import Err

router = APIRouter(prefix="/v1/namespaces", tags=["namespaces"])


@router.get("", response_model=NamespaceListResponse)
async def list_namespaces(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> NamespaceListResponse:
    """List the namespaces the bearer's scope grants access to.

    Reads ``request.state.auth.scopes`` (set by ``authenticate_request``)
    and returns the namespace prefixes. Operator-scoped tokens get the
    string ``"*"`` instead of a per-namespace list.
    """
    requirement = AuthRequirement()  # any valid token; scope-by-scope below
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
    namespaces: list[str] = []
    for scope in ctx.scopes:
        if scope == "operator":
            return NamespaceListResponse(items=["*"])
        # Scopes look like "tenant/presence/plane:rw"
        ns, _, _ = scope.partition(":")
        namespaces.append(ns)
    return NamespaceListResponse(items=sorted(set(namespaces)))


@router.get(
    "/{namespace_path:path}/stats",
    response_model=NamespaceStats,
    dependencies=[Depends(require_auth(namespace_qs_param="namespace_path"))],
)
async def namespace_stats(
    namespace_path: str = Path(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> NamespaceStats:
    """Counts per plane for the given namespace + last activity epoch.

    The ``namespace_path`` arrives URL-encoded (slashes → ``%2F``) per
    standard FastAPI ``:path`` handling.
    """
    counts: dict[str, int] = {}
    last_epoch = 0.0
    for collection, key in (
        ("musubi_episodic", "episodic"),
        ("musubi_curated", "curated"),
        ("musubi_concept", "concept"),
        ("musubi_artifact", "artifact"),
        ("musubi_thought", "thought"),
    ):
        try:
            resp = qdrant.count(
                collection_name=collection,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="namespace", match=models.MatchValue(value=namespace_path)
                        )
                    ]
                ),
                exact=True,
            )
            counts[key] = int(resp.count)
        except Exception:
            counts[key] = 0
    return NamespaceStats(
        namespace=namespace_path,
        counts=counts,
        last_activity_epoch=last_epoch or None,
    )


__all__ = ["router"]
