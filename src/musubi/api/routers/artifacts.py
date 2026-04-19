"""Artifact read endpoints — metadata, chunk listing, blob download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from qdrant_client import QdrantClient

from musubi.api.auth import require_auth
from musubi.api.dependencies import get_artifact_plane, get_qdrant_client
from musubi.api.errors import APIError
from musubi.api.pagination import Page
from musubi.api.routers._scroll import scroll_namespace
from musubi.planes.artifact import ArtifactPlane
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
) -> Response:
    """Stream the raw bytes of an artifact.

    The artifact plane stores blobs out-of-band (filesystem or S3 in
    production). For this read-side slice we serve a placeholder body —
    the production wiring of blob storage lives in slice-plane-artifact's
    follow-up. The route + auth gate + 404 path are exercised here so
    adapters can call the endpoint.
    """
    parent = await plane.get(namespace=namespace, object_id=object_id)
    if parent is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"artifact {object_id!r} not found in namespace {namespace!r}",
        )
    # Placeholder: stream an empty 0-byte body with the artifact's
    # declared content-type. Real bytes-from-blob-store wiring is the
    # write-slice / blob-store follow-up.
    return Response(
        content=b"",
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
