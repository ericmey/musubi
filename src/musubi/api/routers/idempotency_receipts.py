"""Authorization-bound lookup for durable idempotent capture receipts."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator

from musubi.api.auth import authorize_namespace
from musubi.api.dependencies import get_settings_dep
from musubi.api.errors import APIError
from musubi.api.idempotency import IdempotencyLeaseCache, get_idempotency_lease_cache
from musubi.api.idempotency_dependency import build_identity
from musubi.api.idempotency_receipts import (
    RECEIPT_ELIGIBLE_OPERATIONS,
    DurableReceiptStore,
    ReceiptLookupStatus,
    get_idempotency_receipt_store,
)
from musubi.settings import Settings

router = APIRouter(prefix="/v1/idempotency/receipts", tags=["idempotency"])


class ReceiptLookupRequest(BaseModel):
    namespace: str
    method: Literal["POST"]
    operation_id: str
    idempotency_key: str = Field(min_length=1, max_length=256)
    request_digest: str = Field(min_length=64, max_length=64)

    @field_validator("request_digest")
    @classmethod
    def _hex_digest(cls, value: str) -> str:
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("request_digest must be hexadecimal SHA-256") from exc
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


@router.post(
    "/lookup",
    response_model=ReceiptLookupResponse,
    operation_id="lookup_idempotency_receipt.bucket=default",
)
async def lookup_receipt(
    body: ReceiptLookupRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    store: DurableReceiptStore = Depends(get_idempotency_receipt_store),
    lease_cache: IdempotencyLeaseCache = Depends(get_idempotency_lease_cache),
) -> ReceiptLookupResponse:
    # The storage call stays below this explicit authorization edge. An absent receipt and a
    # receipt owned by another principal/namespace are therefore indistinguishable to the caller.
    authorize_namespace(request, body.namespace, settings=settings, access="w")
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
    result = store.lookup_with_lease(
        identity=identity,
        digest=bytes.fromhex(body.request_digest),
        lease_cache=lease_cache,
    )
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


__all__ = ["ReceiptLookupRequest", "ReceiptLookupResponse", "router"]
