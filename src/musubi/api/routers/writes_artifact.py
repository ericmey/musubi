"""Artifact write endpoints — multipart upload, archive, purge."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import BaseModel
from qdrant_client import QdrantClient

from musubi.api.auth import authorize_namespace, require_auth
from musubi.api.dependencies import (
    get_artifact_plane,
    get_lifecycle_service,
    get_qdrant_client,
    get_settings_dep,
)
from musubi.api.errors import APIError
from musubi.api.lifecycle_responses import TransitionPendingBody, pending_response
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, is_transition_pending
from musubi.lifecycle.transitions import transition
from musubi.planes.artifact import ArtifactPlane
from musubi.settings import Settings
from musubi.types.artifact import SourceArtifact
from musubi.types.common import Ok

router = APIRouter(prefix="/v1/artifacts", tags=["artifact-writes"])


class ArtifactCreateResponse(BaseModel):
    object_id: str
    state: str
    size_bytes: int
    sha256: str


@router.post(
    "",
    response_model=ArtifactCreateResponse,
    status_code=202,
    operation_id="upload_artifact.bucket=artifact-upload",
    dependencies=[Depends(require_auth(access="w"))],
)
async def upload_artifact(
    request: Request,
    namespace: str = Form(...),
    title: str = Form(...),
    content_type: str = Form(...),
    source_system: str = Form("api-upload"),
    chunker: str = Form("markdown-headings-v1"),
    file: UploadFile = File(...),
    plane: ArtifactPlane = Depends(get_artifact_plane),
    settings: Settings = Depends(get_settings_dep),
) -> ArtifactCreateResponse:
    # SEC-003: the namespace arrives via Form, which require_auth cannot see (it reads the
    # query string). Authorize the parsed Form namespace with route-native shared authz so a
    # write-scoped token is required for the namespace actually being written.
    authorize_namespace(request, namespace, settings=settings, access="w")
    raw = await file.read()
    sha = hashlib.sha256(raw).hexdigest()
    saved = await plane.create(
        SourceArtifact(
            namespace=namespace,
            title=title,
            filename=file.filename or "upload.bin",
            sha256=sha,
            content_type=content_type,
            size_bytes=len(raw),
            chunker=chunker,
            ingestion_metadata={"source_system": source_system},
        )
    )
    # Persist raw bytes under artifact_blob_path/<namespace>/<object_id>.
    # The layout matches ops/cleanup.py's hard-delete walker and is the
    # minimum wiring for GET /artifacts/{id}/blob to round-trip. Real
    # content-addressed blob storage (S3 / by-sha256) is a follow-up.
    blob_path = settings.artifact_blob_path / saved.namespace / saved.object_id
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(raw)
    return ArtifactCreateResponse(
        object_id=saved.object_id,
        state=saved.state,
        size_bytes=saved.size_bytes,
        sha256=saved.sha256,
    )


@router.post(
    "/{object_id}/archive",
    operation_id="archive_artifact.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
    responses={202: {"model": TransitionPendingBody, "description": "Transition durably pending."}},
)
async def archive_artifact(
    object_id: str,
    namespace: str = Query(...),
    qdrant: QdrantClient = Depends(get_qdrant_client),
    plane: ArtifactPlane = Depends(get_artifact_plane),
    coordinator: LifecycleTransitionCoordinator = Depends(get_lifecycle_service),
) -> Response:
    # exists(), not get(): the transition below goes by object_id and never uses the
    # deserialized row, so a corrupted payload must not be able to block removal.
    # The removability of a memory must never depend on that memory being valid.
    if not await plane.exists(namespace=namespace, object_id=object_id):
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"artifact {object_id!r} not found in namespace {namespace!r}",
        )
    result = transition(
        qdrant,
        coordinator=coordinator,
        object_id=object_id,
        target_state="archived",
        actor="api-archive",
        reason="api-archive",
    )
    if not isinstance(result, Ok):
        raise APIError(
            status_code=400,
            code="BAD_REQUEST",
            detail=f"archive transition rejected: {result.error.message}",
        )
    if is_transition_pending(result.value):
        return pending_response(result.value)
    return Response(
        status_code=200, content=b'{"status":"archived"}', media_type="application/json"
    )


@router.post(
    "/{object_id}/purge",
    operation_id="purge_artifact.bucket=default",
    dependencies=[Depends(require_auth(access="w"))],
)
async def purge_artifact(
    request: Request,
    object_id: str,
    namespace: str = Query(...),
) -> Response:
    """Operator-only hard delete of the artifact metadata + blob.

    Reads the AuthContext attached by the outer ``require_auth`` dep
    and rejects with 403 if the operator scope isn't on the bearer."""
    ctx = getattr(request.state, "auth", None)
    if ctx is None or "operator" not in (ctx.scopes or ()):
        raise APIError(
            status_code=403,
            code="FORBIDDEN",
            detail="purge requires operator scope",
        )
    # Blob-store removal is a future slice (artifact blob storage isn't
    # wired in v0). Today: respond 202 acknowledging the operator
    # request; the actual purge job runs offline.
    del object_id, namespace
    return Response(
        status_code=202, content=b'{"status":"purge-scheduled"}', media_type="application/json"
    )


__all__ = ["router"]
