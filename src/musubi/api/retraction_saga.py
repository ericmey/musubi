"""IDEM-007 escrow-first episodic retraction saga.

Authorization precedes every observation. Exact bytes become a stored-unindexed
artifact before one evidence-gated, non-reembedding episodic CAS is attempted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import regex
from fastapi import Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from qdrant_client import QdrantClient

from musubi.api.auth import authorize_namespace
from musubi.api.dependencies import get_settings_dep
from musubi.api.errors import APIError
from musubi.api.idempotency_dependency import IdempotentContext
from musubi.api.idempotency_receipts import receipt_identity_hash
from musubi.api.write_auth import AuthorizedWrite
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.artifact.escrow import ArtifactEscrowWriter, derive_escrow_address
from musubi.planes.episodic import EpisodicPlane
from musubi.settings import Settings
from musubi.store.immutable_vectors import (
    NonEmbeddingPatchConflict,
    retract_non_embedding_payload,
)
from musubi.store.raw_lookup import retrieve_by_point_id
from musubi.store.retraction_evidence import retraction_evidence_binding_errors
from musubi.store.specs import strip_layout_fields
from musubi.types.common import Err
from musubi.types.episodic import EpisodicMemory, RetractionArtifactRef, RetractionEvidence

EPISODIC_CONTENT_LIMIT_BYTES = 32_768
MIN_RETRACTION_PREFIX_UTF8_BYTES = 256


class RetractEpisodicRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    expected_version: int = Field(ge=0)
    on: str = Field(min_length=1)
    because: str = Field(min_length=1)
    truth: str = Field(min_length=1)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=128)


class RetractEpisodicResponse(BaseModel):
    object_id: str
    version: int
    artifact_ref: RetractionArtifactRef
    retraction_evidence: RetractionEvidence


def _artifact_namespace(source_namespace: str) -> str:
    parts = source_namespace.split("/")
    if len(parts) != 3 or parts[2] != "episodic":
        raise APIError(
            status_code=422,
            code="BAD_REQUEST",
            detail="retraction namespace must be an episodic namespace",
        )
    return f"{parts[0]}/{parts[1]}/artifact"


async def authorized_retract(
    request: Request,
    body: RetractEpisodicRequest = Body(...),
    settings: Settings = Depends(get_settings_dep),
) -> AuthorizedWrite[RetractEpisodicRequest]:
    """Authorize both planes before any idempotency or storage observation."""
    authorize_namespace(request, body.namespace, settings=settings, access="w")
    authorize_namespace(request, _artifact_namespace(body.namespace), settings=settings, access="w")
    return AuthorizedWrite(auth=request.state.auth, namespace=body.namespace, body=body)


def _stored_state_error(detail: str) -> APIError:
    return APIError(
        status_code=409,
        code="CONFLICT",
        detail=f"stored episodic state is malformed; retraction refused before escrow: {detail}",
    )


@dataclass(frozen=True)
class _StoredOriginal:
    raw: dict[str, Any]
    target: dict[str, Any]
    logical: EpisodicMemory
    original: bytes
    is_v2: bool


async def _read_original(
    *,
    plane: EpisodicPlane,
    qdrant: QdrantClient,
    namespace: str,
    object_id: str,
) -> _StoredOriginal:
    raw = await plane.raw_payload(namespace=namespace, object_id=object_id)
    if raw is None:
        raise APIError(
            status_code=404,
            code="NOT_FOUND",
            detail=f"episodic {object_id!r} not found in namespace {namespace!r}",
        )
    is_v2 = raw.get("point_kind") == "anchor"
    if raw.get("point_kind") not in {None, "anchor"}:
        raise _stored_state_error("identity row has an invalid point_kind")
    target = raw
    if is_v2:
        live_point = raw.get("live_point")
        if not isinstance(live_point, str) or not live_point:
            raise _stored_state_error("v2 anchor has no valid live_point")
        target = (
            retrieve_by_point_id(
                qdrant,
                "musubi_episodic",
                point_id=live_point,
            )
            or {}
        )
        if (
            target.get("point_kind") != "content"
            or target.get("namespace") != namespace
            or target.get("object_id") != object_id
            or target.get("generation") != raw.get("committed_operation_id")
        ):
            raise _stored_state_error("v2 anchor does not resolve to its committed content")
    original_text = target.get("content")
    if not isinstance(original_text, str) or not original_text:
        raise _stored_state_error("original content is missing or not text")
    try:
        logical = EpisodicMemory.model_validate(strip_layout_fields({**target, **raw}))
    except ValidationError as exc:
        raise _stored_state_error("identity row does not satisfy the episodic model") from exc
    return _StoredOriginal(
        raw=raw,
        target=target,
        logical=logical,
        original=original_text.encode("utf-8"),
        is_v2=is_v2,
    )


def _render_tombstone(
    *, body: RetractEpisodicRequest, original: bytes, artifact_id: str
) -> tuple[str, str, int]:
    """Return bounded content, literal grapheme-safe prefix, and prefix bytes."""
    try:
        original_text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _stored_state_error("original content is not valid UTF-8") from exc

    def render(prefix: str) -> str:
        return (
            f"RETRACTED {body.on}. This memory was FALSE. Do not act on it.\n"
            f"Why it is false: {body.because}\n"
            f"Original excerpt:\n{prefix}\n"
            f"Corrected truth:\n{body.truth}\n"
            f"The complete original is preserved in artifact {artifact_id}."
        )

    empty_bytes = len(render("").encode("utf-8"))
    available = EPISODIC_CONTENT_LIMIT_BYTES - empty_bytes
    required = min(len(original), MIN_RETRACTION_PREFIX_UTF8_BYTES)
    if available < required:
        raise APIError(
            status_code=422,
            code="CONTENT_TOO_LARGE",
            detail=(
                "caller prose leaves fewer than the 256 UTF-8 bytes reserved for "
                "the storage-derived original prefix"
            ),
        )
    prefix_parts: list[str] = []
    prefix_bytes = 0
    for grapheme in regex.findall(r"\X", original_text):
        encoded = grapheme.encode("utf-8")
        if prefix_bytes + len(encoded) > available:
            break
        prefix_parts.append(grapheme)
        prefix_bytes += len(encoded)
        if prefix_bytes >= required:
            break
    if prefix_bytes < required or not prefix_parts:
        raise APIError(
            status_code=422,
            code="CONTENT_TOO_LARGE",
            detail="one complete original grapheme cannot fit the bounded tombstone",
        )
    prefix = "".join(prefix_parts)
    tombstone = render(prefix)
    if len(tombstone.encode("utf-8")) > EPISODIC_CONTENT_LIMIT_BYTES:
        raise APIError(
            status_code=422,
            code="CONTENT_TOO_LARGE",
            detail="server-built tombstone exceeds the episodic UTF-8 byte limit",
        )
    return tombstone, prefix, prefix_bytes


def _validate_adopted_artifact(
    *,
    evidence: RetractionEvidence,
    namespace: str,
    object_id: str,
    head: Any,
    blob: bytes,
) -> None:
    address = derive_escrow_address(
        source_namespace=namespace,
        source_object_id=object_id,
        original_sha256=evidence.original_sha256,
    )
    if (
        evidence.artifact_namespace != address.artifact_namespace
        or evidence.artifact_ref.artifact_id != address.artifact_id
        or head is None
        or head.artifact_state != "stored_unindexed"
        or head.sha256 != evidence.original_sha256
        or head.size_bytes != evidence.original_utf8_bytes
        or hashlib.sha256(blob).hexdigest() != evidence.original_sha256
        or len(blob) != evidence.original_utf8_bytes
    ):
        raise APIError(
            status_code=409,
            code="CONFLICT",
            detail="committed retraction evidence does not resolve to its exact escrow",
        )


async def execute_retraction(
    *,
    request: Request,
    object_id: str,
    ctx: IdempotentContext,
    qdrant: QdrantClient,
    episodic: EpisodicPlane,
    artifact: ArtifactPlane,
    settings: Settings,
) -> RetractEpisodicResponse:
    body = ctx.body
    assert isinstance(body, RetractEpisodicRequest)
    if ctx.identity is None:
        raise APIError(status_code=500, code="INTERNAL", detail="required idempotency is absent")
    idem_state = getattr(request.state, "idem", None)
    if idem_state is None:
        raise APIError(status_code=500, code="INTERNAL", detail="idempotency state is absent")
    operation_hash = receipt_identity_hash(ctx.identity)
    request_digest = bytes(idem_state.digest).hex()

    raw = await episodic.raw_payload(namespace=body.namespace, object_id=object_id)
    if raw is not None and "retraction_evidence" in raw:
        try:
            evidence = RetractionEvidence.model_validate(raw.get("retraction_evidence"))
        except ValidationError as exc:
            raise APIError(
                status_code=409,
                code="CONFLICT",
                detail="stored retraction evidence is unparseable; operator repair is required",
            ) from exc
        if (
            evidence.operation_identity_hash != operation_hash
            or evidence.request_digest != request_digest
        ):
            raise APIError(
                status_code=409,
                code="CONFLICT",
                detail="retraction identity or request digest differs from committed evidence",
            )
        stored = await _read_original(
            plane=episodic,
            qdrant=qdrant,
            namespace=body.namespace,
            object_id=object_id,
        )
        artifact_id = evidence.artifact_ref.artifact_id
        try:
            head = await artifact.get(
                namespace=evidence.artifact_namespace,
                object_id=artifact_id,
            )
            blob = (
                settings.artifact_blob_path / evidence.artifact_namespace / artifact_id
            ).read_bytes()
        except (OSError, ValidationError) as exc:
            raise APIError(
                status_code=409,
                code="CONFLICT",
                detail="committed retraction evidence has unreadable escrow state",
            ) from exc
        _validate_adopted_artifact(
            evidence=evidence,
            namespace=body.namespace,
            object_id=object_id,
            head=head,
            blob=blob,
        )
        if stored.is_v2:
            errors = retraction_evidence_binding_errors(
                evidence=evidence,
                anchor_payload=stored.raw,
                target_payload=stored.target,
            )
            if errors:
                raise APIError(
                    status_code=409,
                    code="CONFLICT",
                    detail="committed retraction evidence no longer binds episodic storage",
                )
        else:
            prefix = blob[: evidence.quoted_prefix_utf8_bytes]
            try:
                prefix_text = prefix.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise APIError(
                    status_code=409,
                    code="CONFLICT",
                    detail="committed retraction prefix is not valid UTF-8",
                ) from exc
            if prefix_text not in stored.logical.content:
                raise APIError(
                    status_code=409,
                    code="CONFLICT",
                    detail="committed retraction prefix is absent from episodic storage",
                )
        return RetractEpisodicResponse(
            object_id=object_id,
            version=stored.logical.version,
            artifact_ref=evidence.artifact_ref,
            retraction_evidence=evidence,
        )

    stored = await _read_original(
        plane=episodic,
        qdrant=qdrant,
        namespace=body.namespace,
        object_id=object_id,
    )
    address = derive_escrow_address(
        source_namespace=body.namespace,
        source_object_id=object_id,
        original_sha256=hashlib.sha256(stored.original).hexdigest(),
    )
    tombstone, _prefix, prefix_bytes = _render_tombstone(
        body=body,
        original=stored.original,
        artifact_id=address.artifact_id,
    )
    pointer: dict[str, str]
    if stored.is_v2:
        pointer = {"kind": "v2", "live_point": str(stored.raw.get("live_point"))}
    else:
        pointer = {"kind": "legacy_self"}
    evidence = RetractionEvidence.model_validate(
        {
            "kind": "artifact_escrow_v1",
            "artifact_namespace": address.artifact_namespace,
            "artifact_ref": {"artifact_id": address.artifact_id},
            "original_sha256": hashlib.sha256(stored.original).hexdigest(),
            "original_utf8_bytes": len(stored.original),
            "quoted_prefix_utf8_bytes": prefix_bytes,
            "omitted_bytes": len(stored.original) - prefix_bytes,
            "vector_basis": "original",
            "preserved_pointer": pointer,
            "operation_identity_hash": operation_hash,
            "request_digest": request_digest,
        }
    )
    changes = {
        "content": tombstone,
        "summary": body.summary or "Retracted false memory",
        "tags": sorted(set(body.tags) | {"retracted"}),
        "importance": 1,
    }
    # Validate the exact would-be canonical row before durable escrow or CAS.
    # A post-CAS validation would report an unreadable row only after persisting it.
    try:
        EpisodicMemory.model_validate(
            strip_layout_fields(
                {
                    **stored.target,
                    **stored.raw,
                    **changes,
                    "retraction_evidence": evidence.model_dump(mode="json"),
                }
            )
        )
    except ValidationError as exc:
        raise APIError(
            status_code=422,
            code="BAD_REQUEST",
            detail="retraction would produce an unreadable episodic row",
        ) from exc

    writer = ArtifactEscrowWriter(plane=artifact, blob_root=settings.artifact_blob_path)
    escrow = await writer.store(
        source_namespace=body.namespace,
        source_object_id=object_id,
        original=stored.original,
    )
    if isinstance(escrow, Err):
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail=f"escrow {escrow.error.code}: {escrow.error.detail}",
        )
    head = escrow.value
    if head.object_id != address.artifact_id or head.namespace != address.artifact_namespace:
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="escrow returned a head outside its deterministic address",
        )
    observed_version = int(stored.raw.get("version", 0))
    if body.expected_version != observed_version:
        raise APIError(
            status_code=409,
            code="CONFLICT",
            detail=(
                f"retraction expected version {body.expected_version} but observed "
                f"{observed_version}; verified escrow is preserved for exact retry"
            ),
        )
    try:
        published = retract_non_embedding_payload(
            qdrant,
            "musubi_episodic",
            namespace=body.namespace,
            object_id=object_id,
            observed_payload=stored.raw,
            target_payload=stored.target,
            changes=changes,
            evidence=evidence,
        )
    except NonEmbeddingPatchConflict as exc:
        raise APIError(status_code=409, code="CONFLICT", detail=str(exc)) from exc
    except ValueError as exc:
        raise APIError(
            status_code=409,
            code="CONFLICT",
            detail=f"retraction evidence refused before commit: {exc}",
        ) from exc
    except OSError as exc:
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="verified escrow exists but episodic retraction did not commit",
        ) from exc
    logical = EpisodicMemory.model_validate(strip_layout_fields(published))
    assert logical.retraction_evidence is not None
    return RetractEpisodicResponse(
        object_id=object_id,
        version=logical.version,
        artifact_ref=evidence.artifact_ref,
        retraction_evidence=evidence,
    )


__all__ = [
    "EPISODIC_CONTENT_LIMIT_BYTES",
    "MIN_RETRACTION_PREFIX_UTF8_BYTES",
    "RetractEpisodicRequest",
    "RetractEpisodicResponse",
    "authorized_retract",
    "execute_retraction",
]
