"""Artifact read endpoints — metadata, chunk listing, blob download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from qdrant_client import QdrantClient

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_artifact_plane, get_qdrant_client, get_settings_dep
from musubi.api.errors import APIError
from musubi.api.pagination import Page
from musubi.api.routers._scroll import scroll_namespace
from musubi.planes.artifact import ArtifactPlane
from musubi.settings import Settings
from musubi.types.artifact import ArtifactChunk, SourceArtifact

router = APIRouter(prefix="/v1/artifacts", tags=["artifact"])


@router.get(
    "/{object_id}",
    response_model=SourceArtifact,
    dependencies=[Depends(require_auth())],
)
async def get_artifact(
    object_id: str,
    namespace: str = Query(...),
    plane: ArtifactPlane = Depends(get_artifact_plane),
) -> SourceArtifact:
    fetched = await plane.get(namespace=namespace, object_id=object_id)
    if fetched is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"artifact {object_id!r} not found in namespace {namespace!r}",
        )
    return fetched


@router.get(
    "/{object_id}/chunks",
    response_model=list[ArtifactChunk],
    dependencies=[Depends(require_auth())],
)
async def get_artifact_chunks(
    object_id: str,
    namespace: str = Query(...),
    plane: ArtifactPlane = Depends(get_artifact_plane),
) -> list[ArtifactChunk]:
    # Confirm the parent artifact exists in the namespace; the plane's
    # query_by_artifact is namespace-agnostic, so we gate it here.
    parent = await plane.get(namespace=namespace, object_id=object_id)
    if parent is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"artifact {object_id!r} not found in namespace {namespace!r}",
        )
    return await plane.query_by_artifact(artifact_id=object_id)


@router.get(
    "/{object_id}/blob",
    dependencies=[Depends(require_auth())],
)
async def get_artifact_blob(
    object_id: str,
    namespace: str = Query(...),
    plane: ArtifactPlane = Depends(get_artifact_plane),
    settings: Settings = Depends(get_settings_dep),
) -> Response:
    """Stream the raw bytes of an artifact.

    Bytes are served from ``settings.artifact_blob_path/<namespace>/<object_id>``
    — the same layout the cleanup worker walks (ops/cleanup.py). The
    write path lives in ``writes_artifact.upload_artifact``. Content-
    addressed storage (by sha256, S3 backend) is a follow-up; this
    slice is the minimum viable round-trip so adapters can retrieve
    what they uploaded.
    """
    parent = await plane.get(namespace=namespace, object_id=object_id)
    if parent is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"artifact {object_id!r} not found in namespace {namespace!r}",
        )
    blob_path = settings.artifact_blob_path / namespace / object_id
    if not blob_path.exists():
        # Metadata exists but bytes don't — either the artifact was
        # created before blob persistence wiring landed, or the blob
        # was garbage-collected. Surface 404 rather than a misleading
        # empty body so callers can distinguish "missing" from "empty".
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"blob for artifact {object_id!r} not found",
        )
    return Response(
        content=blob_path.read_bytes(),
        media_type=getattr(parent, "content_type", "application/octet-stream"),
    )


@router.get(
    "",
    response_model=Page[SourceArtifact],
    dependencies=[Depends(require_auth())],
)
async def list_artifacts(
    namespace: str = Query(...),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = Query(None),
    qdrant: QdrantClient = Depends(get_qdrant_client),
) -> Page[SourceArtifact]:
    items, next_cursor = scroll_namespace(
        qdrant,
        collection="musubi_artifact",
        namespace=namespace,
        limit=limit,
        cursor=cursor,
    )
    return Page[SourceArtifact](
        items=[SourceArtifact.model_validate(p) for p in items],
        next_cursor=next_cursor,
    )


__all__ = ["router"]
