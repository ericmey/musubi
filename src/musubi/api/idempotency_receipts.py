"""Durable completed-response receipts for external idempotent clients."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from fastapi import Request

from musubi.api.idempotency import CompletedResponse, IdempotencyLeaseCache

RECEIPT_ELIGIBLE_OPERATIONS = frozenset(
    {
        "capture_episodic.bucket=capture",
        "create_curated.bucket=capture",
    }
)


class ReceiptLookupStatus(StrEnum):
    FOUND = "found"
    ABSENT = "absent"
    CONFLICT = "conflict"
    IN_FLIGHT = "in_flight"


@dataclass(frozen=True)
class DurableReceipt:
    object_id: str
    namespace: str
    operation: str
    response_status: int
    response_sha256: str


@dataclass(frozen=True)
class ReceiptLookup:
    status: ReceiptLookupStatus
    receipt: DurableReceipt | None = None


def _identity_hash(identity: tuple[Any, ...]) -> str:
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(b"musubi-idem-receipt-v1\x00" + encoded).hexdigest()


class DurableReceiptStore:
    """SQLite/WAL ledger retained independently from ordinary replay TTL."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._closed = False
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise RuntimeError("receipt store is closed")
        connection = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_receipts (
                    identity_hash TEXT PRIMARY KEY,
                    request_digest BLOB NOT NULL,
                    namespace TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    response_status INTEGER NOT NULL,
                    response_headers_json TEXT NOT NULL,
                    response_body BLOB NOT NULL,
                    response_sha256 TEXT NOT NULL,
                    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _object_id(response: CompletedResponse) -> str:
        try:
            decoded = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("idempotent success response is not JSON") from exc
        object_id = decoded.get("object_id") if isinstance(decoded, dict) else None
        if not isinstance(object_id, str) or not object_id:
            raise ValueError("idempotent success response omitted object_id")
        return object_id

    def store(
        self,
        *,
        identity: tuple[Any, ...],
        digest: bytes,
        response: CompletedResponse,
        namespace: str,
        operation: str,
    ) -> None:
        if len(digest) != 32 or not 200 <= response.status < 300:
            raise ValueError("only exact successful idempotent responses can become receipts")
        object_id = self._object_id(response)
        headers = json.dumps(
            [
                [base64.b64encode(key).decode("ascii"), base64.b64encode(value).decode("ascii")]
                for key, value in response.raw_headers
            ],
            separators=(",", ":"),
        )
        response_sha = hashlib.sha256(response.body).hexdigest()
        key = _identity_hash(identity)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO idempotency_receipts
                (identity_hash, request_digest, namespace, operation, object_id,
                 response_status, response_headers_json, response_body, response_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    digest,
                    namespace,
                    operation,
                    object_id,
                    response.status,
                    headers,
                    response.body,
                    response_sha,
                ),
            )
            if cursor.rowcount != 1:
                existing = connection.execute(
                    """
                    SELECT request_digest, namespace, operation, object_id, response_status,
                           response_headers_json, response_body, response_sha256
                      FROM idempotency_receipts WHERE identity_hash = ?
                    """,
                    (key,),
                ).fetchone()
                expected = (
                    digest,
                    namespace,
                    operation,
                    object_id,
                    response.status,
                    headers,
                    response.body,
                    response_sha,
                )
                if existing is None or tuple(existing) != expected:
                    raise ValueError("durable receipt collision has divergent content")

    def lookup(self, *, identity: tuple[Any, ...], digest: bytes) -> ReceiptLookup:
        if len(digest) != 32:
            raise ValueError("request digest must be SHA-256")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT request_digest, namespace, operation, object_id,
                       response_status, response_sha256
                  FROM idempotency_receipts WHERE identity_hash = ?
                """,
                (_identity_hash(identity),),
            ).fetchone()
        if row is None:
            return ReceiptLookup(ReceiptLookupStatus.ABSENT)
        if row["request_digest"] != digest:
            return ReceiptLookup(ReceiptLookupStatus.CONFLICT)
        return ReceiptLookup(
            ReceiptLookupStatus.FOUND,
            DurableReceipt(
                object_id=row["object_id"],
                namespace=row["namespace"],
                operation=row["operation"],
                response_status=row["response_status"],
                response_sha256=row["response_sha256"],
            ),
        )

    def lookup_with_lease(
        self,
        *,
        identity: tuple[Any, ...],
        digest: bytes,
        lease_cache: IdempotencyLeaseCache,
    ) -> ReceiptLookup:
        durable = self.lookup(identity=identity, digest=digest)
        if durable.status is not ReceiptLookupStatus.ABSENT:
            return durable
        lease_status = lease_cache.probe(identity, digest=digest)
        if lease_status == "in_flight":
            return ReceiptLookup(ReceiptLookupStatus.IN_FLIGHT)
        if lease_status == "conflict":
            return ReceiptLookup(ReceiptLookupStatus.CONFLICT)
        return durable

    def close(self) -> None:
        self._closed = True


def get_idempotency_receipt_store(request: Request) -> DurableReceiptStore:
    store = getattr(request.app.state, "idempotency_receipt_store", None)
    if store is None:
        raise RuntimeError("idempotency receipt store is not configured")
    return cast(DurableReceiptStore, store)


__all__ = [
    "RECEIPT_ELIGIBLE_OPERATIONS",
    "DurableReceipt",
    "DurableReceiptStore",
    "ReceiptLookup",
    "ReceiptLookupStatus",
    "get_idempotency_receipt_store",
]
