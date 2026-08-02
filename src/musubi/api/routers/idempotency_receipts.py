"""Authorization-bound lookup for durable idempotent capture receipts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from musubi.api.auth import authorize_namespace, require_operator_scope
from musubi.api.dependencies import get_settings_dep
from musubi.api.errors import APIError
from musubi.api.idempotency import IdempotencyLeaseCache, get_idempotency_lease_cache
from musubi.api.idempotency_dependency import build_identity
from musubi.api.idempotency_receipts import (
    RECEIPT_ELIGIBLE_OPERATIONS,
    DurableReceiptStore,
    ReceiptLookupStatus,
    get_idempotency_receipt_store,
    receipt_identity_hash,
)
from musubi.settings import Settings

router = APIRouter(prefix="/v1/idempotency/receipts", tags=["idempotency"])


class ReceiptLookupRequest(BaseModel):
    namespace: str
    method: Literal["POST"]
    operation_id: str
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("request_digest")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        try:
            decoded = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("request_digest must be hexadecimal SHA-256") from exc
        if len(decoded) != 32:
            raise ValueError("request_digest must decode to exactly 32 bytes")
        return value.lower()

    @field_validator("operation_id")
    @classmethod
    def _eligible_operation(cls, value: str) -> str:
        if value not in RECEIPT_ELIGIBLE_OPERATIONS:
            raise ValueError("operation_id is not receipt-eligible")
        return value


class ReceiptLookupResponse(BaseModel):
    status: ReceiptLookupStatus
    object_id: str | None = None
    namespace: str | None = None
    operation_id: str | None = None
    response_status: int | None = None
    response_sha256: str | None = None


class ReceiptAuditRequest(ReceiptLookupRequest):
    target_issuer: str = Field(min_length=1, max_length=512)
    target_subject: str = Field(min_length=1, max_length=256)
    target_presence: str = Field(min_length=1, max_length=256)


class ReceiptAuditResponse(BaseModel):
    status: Literal["found", "absent", "conflict"]
    observer_attestation: Literal["server_attested"] = "server_attested"
    observer_issuer: str
    observer_subject: str
    observer_presence: str
    observer_effective_scopes: tuple[str, ...]
    observed_at: datetime
    namespace: str
    operation_id: str
    request_digest: str
    target_identity_hash: str
    object_id: str | None = None
    response_status: int | None = None
    response_sha256: str | None = None
    receipt_committed_at: str | None = None


@router.post(
    "/lookup",
    response_model=ReceiptLookupResponse,
    operation_id="lookup_idempotency_receipt.bucket=default",
)
async def lookup_receipt(
    body: ReceiptLookupRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    lease_cache: IdempotencyLeaseCache = Depends(get_idempotency_lease_cache),
) -> ReceiptLookupResponse:
    # The storage call stays below this explicit authorization edge. An absent receipt and a
    # receipt owned by another principal/namespace are therefore indistinguishable to the caller.
    authorize_namespace(request, body.namespace, settings=settings, access="w")
    try:
        store: DurableReceiptStore = get_idempotency_receipt_store(request)
    except RuntimeError as exc:
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="durable idempotency receipts are unavailable",
        ) from exc
    auth = getattr(request.state, "auth", None)
    if auth is None:
        raise APIError(status_code=500, code="INTERNAL", detail="authorized identity unavailable")
    identity = build_identity(
        auth,
        body.method,
        body.operation_id,
        body.namespace,
        body.idempotency_key,
    )
    try:
        result = store.lookup_with_lease(
            identity=identity,
            digest=bytes.fromhex(body.request_digest),
            lease_cache=lease_cache,
        )
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="durable idempotency receipts are unavailable",
        ) from exc
    if result.receipt is None:
        return ReceiptLookupResponse(status=result.status)
    receipt = result.receipt
    return ReceiptLookupResponse(
        status=result.status,
        object_id=receipt.object_id,
        namespace=receipt.namespace,
        operation_id=receipt.operation,
        response_status=receipt.response_status,
        response_sha256=receipt.response_sha256,
    )


@router.post(
    "/audit",
    response_model=ReceiptAuditResponse,
    operation_id="audit_idempotency_receipt.bucket=default",
)
async def audit_receipt(
    body: ReceiptAuditRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
) -> ReceiptAuditResponse:
    """Confirm one exact principal-bound durable receipt for a two-seat audit."""
    # Both gates run before the store is resolved. Read authority establishes the
    # namespace boundary; operator authority establishes the household audit trust
    # boundary. Neither one implies the other.
    authorize_namespace(request, body.namespace, settings=settings, access="r")
    require_operator_scope(request, settings=settings)
    auth = getattr(request.state, "auth", None)
    if auth is None:
        raise APIError(status_code=500, code="INTERNAL", detail="authorized identity unavailable")
    try:
        store: DurableReceiptStore = get_idempotency_receipt_store(request)
    except RuntimeError as exc:
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="durable idempotency receipts are unavailable",
        ) from exc

    identity = (
        body.target_issuer,
        body.target_subject,
        body.target_presence,
        body.method,
        body.operation_id,
        body.namespace,
        body.idempotency_key,
    )
    try:
        result = store.lookup(identity=identity, digest=bytes.fromhex(body.request_digest))
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise APIError(
            status_code=503,
            code="BACKEND_UNAVAILABLE",
            detail="durable idempotency receipts are unavailable",
        ) from exc

    owns_target = (
        auth.issuer,
        auth.subject,
        auth.presence,
    ) == (
        body.target_issuer,
        body.target_subject,
        body.target_presence,
    )
    status: Literal["found", "absent", "conflict"]
    if result.status is ReceiptLookupStatus.CONFLICT and not owns_target:
        # A cross-principal conflict would reveal that a guessed human-readable key
        # exists with different content. Collapse it to absent; only an owner may see
        # conflict fidelity for its own identity.
        status = "absent"
    elif result.status is ReceiptLookupStatus.CONFLICT:
        status = "conflict"
    elif result.status is ReceiptLookupStatus.FOUND:
        status = "found"
    else:
        status = "absent"

    receipt = result.receipt if status == "found" else None
    return ReceiptAuditResponse(
        status=status,
        observer_issuer=auth.issuer,
        observer_subject=auth.subject,
        observer_presence=auth.presence,
        observer_effective_scopes=auth.scopes,
        observed_at=datetime.now(UTC),
        namespace=body.namespace,
        operation_id=body.operation_id,
        request_digest=body.request_digest,
        target_identity_hash=receipt_identity_hash(identity),
        object_id=receipt.object_id if receipt is not None else None,
        response_status=receipt.response_status if receipt is not None else None,
        response_sha256=receipt.response_sha256 if receipt is not None else None,
        receipt_committed_at=receipt.committed_at if receipt is not None else None,
    )


__all__ = [
    "ReceiptAuditRequest",
    "ReceiptAuditResponse",
    "ReceiptLookupRequest",
    "ReceiptLookupResponse",
    "router",
]
