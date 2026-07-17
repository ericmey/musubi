"""IDEM-003 durable, authorization-bound completed-response receipts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from musubi.api.idempotency import CompletedResponse, IdempotencyLeaseCache
from musubi.api.idempotency_dependency import build_identity, canonical_digest
from musubi.api.idempotency_receipts import DurableReceiptStore, ReceiptLookupStatus
from musubi.settings import Settings


NAMESPACE = "eric/claude-code/episodic"
OPERATION = "capture_episodic.bucket=capture"
KEY = "codex-session-turn-1"
RAW_CAPTURE = json.dumps(
    {"namespace": NAMESPACE, "content": "The receipt must survive my client crash."},
    separators=(",", ":"),
).encode()
DIGEST = canonical_digest(RAW_CAPTURE, "application/json")
IDENTITY = (
    "https://auth.example.test",
    "eric-claude-code",
    "eric/claude-code",
    "POST",
    OPERATION,
    NAMESPACE,
    KEY,
)
RESPONSE = CompletedResponse(
    status=202,
    raw_headers=((b"content-type", b"application/json"),),
    body=b'{"object_id":"ep-receipt-1","state":"provisional"}',
)


def _lookup_body(*, digest: bytes = DIGEST, operation: str = OPERATION) -> dict[str, str]:
    return {
        "namespace": NAMESPACE,
        "method": "POST",
        "operation_id": operation,
        "idempotency_key": KEY,
        "request_digest": digest.hex(),
    }


def test_receipt_survives_replay_cache_expiry_and_process_recreation(tmp_path: Path) -> None:
    path = tmp_path / "receipts.sqlite"
    first = DurableReceiptStore(path)
    first.store(
        identity=IDENTITY,
        digest=DIGEST,
        response=RESPONSE,
        namespace=NAMESPACE,
        operation=OPERATION,
    )
    first.close()

    recreated = DurableReceiptStore(path)
    found = recreated.lookup(identity=IDENTITY, digest=DIGEST)
    assert found.status is ReceiptLookupStatus.FOUND
    assert found.receipt is not None
    assert found.receipt.object_id == "ep-receipt-1"
    recreated.close()


class _ExplodingLookupStore:
    def __init__(self) -> None:
        self.lookups = 0

    def lookup(self, **_kwargs: object) -> object:
        self.lookups += 1
        raise AssertionError("receipt storage was touched before authorization")


def test_receipt_lookup_requires_authentication_before_storage_access(
    app_factory: Any,
) -> None:
    store = _ExplodingLookupStore()
    app_factory.state.idempotency_receipt_store = store
    with TestClient(app_factory) as client:
        response = client.post("/v1/idempotency/receipts/lookup", json=_lookup_body())
    assert response.status_code == 401
    assert store.lookups == 0


def test_receipt_lookup_rejects_cross_namespace_access_without_disclosure(
    app_factory: Any,
    out_of_scope_token: str,
) -> None:
    store = _ExplodingLookupStore()
    app_factory.state.idempotency_receipt_store = store
    with TestClient(app_factory) as client:
        response = client.post(
            "/v1/idempotency/receipts/lookup",
            json=_lookup_body(),
            headers={"Authorization": f"Bearer {out_of_scope_token}"},
        )
    assert response.status_code == 403
    assert store.lookups == 0
    assert "ep-receipt-1" not in response.text


def test_receipt_lookup_binds_operation_key_and_request_digest(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts.sqlite")
    store.store(
        identity=IDENTITY,
        digest=DIGEST,
        response=RESPONSE,
        namespace=NAMESPACE,
        operation=OPERATION,
    )
    assert store.lookup(identity=IDENTITY, digest=b"x" * 32).status is ReceiptLookupStatus.CONFLICT
    other_identity = (*IDENTITY[:4], "batch_capture.bucket=batch-write", *IDENTITY[5:])
    assert store.lookup(identity=other_identity, digest=DIGEST).status is ReceiptLookupStatus.ABSENT
    store.close()


class _OrderingReceiptStore:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    def store(self, **_kwargs: object) -> None:
        self.events.append("receipt-commit")
        if self.fail:
            raise OSError("disk unavailable")


async def _exercise_observer(store: _OrderingReceiptStore) -> tuple[list[str], list[dict[str, Any]]]:
    from musubi.api.idempotency import IdempotencyRequestState
    from musubi.api.idempotency_observer import IdempotencyObserver

    events = store.events
    cache = IdempotencyLeaseCache()
    owner = "owner-1"
    assert cache.acquire(IDENTITY, owner, digest=DIGEST)[0] == "acquired"

    async def app(scope: dict[str, Any], _receive: Any, send: Any) -> None:
        scope["state"]["idem"] = IdempotencyRequestState(
            identity=IDENTITY, owner=owner, digest=DIGEST
        )
        scope["state"]["idem_cache"] = cache
        scope["state"]["idem_receipt_store"] = store
        scope["state"]["idem_namespace"] = NAMESPACE
        scope["state"]["idem_operation"] = OPERATION
        await send({"type": "http.response.start", "status": 202, "headers": []})
        await send({"type": "http.response.body", "body": RESPONSE.body, "more_body": False})

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        events.append(f"send:{message['type']}:{message.get('status', '')}")
        sent.append(message)

    scope: dict[str, Any] = {"type": "http", "state": {}, "method": "POST", "path": "/v1/episodic"}
    await IdempotencyObserver(app)(scope, None, send)  # type: ignore[arg-type]
    return events, sent


async def test_success_response_is_not_released_before_durable_receipt_commit() -> None:
    events: list[str] = []
    await _exercise_observer(_OrderingReceiptStore(events))
    assert events[0] == "receipt-commit"
    assert events[1].startswith("send:http.response.start:202")


async def test_receipt_store_failure_returns_failure_not_unreceipted_success() -> None:
    events: list[str] = []
    _, sent = await _exercise_observer(_OrderingReceiptStore(events, fail=True))
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert [message["status"] for message in starts] == [503]
    assert all(message.get("status") != 202 for message in starts)


def test_absent_and_in_flight_are_distinct_from_found(tmp_path: Path) -> None:
    store = DurableReceiptStore(tmp_path / "receipts.sqlite")
    cache = IdempotencyLeaseCache()
    assert store.lookup(identity=IDENTITY, digest=DIGEST).status is ReceiptLookupStatus.ABSENT
    assert cache.acquire(IDENTITY, "owner-1", digest=DIGEST)[0] == "acquired"
    status = store.lookup_with_lease(identity=IDENTITY, digest=DIGEST, lease_cache=cache)
    assert status.status is ReceiptLookupStatus.IN_FLIGHT
    store.close()


def test_exact_object_id_namespace_and_response_hash_round_trip(
    app_factory: Any,
    api_settings: Settings,
    auth: dict[str, str],
) -> None:
    receipt_path = Path(api_settings.lifecycle_sqlite_path).with_name("idempotency-receipts.sqlite")
    app_factory.state.idempotency_receipt_store = DurableReceiptStore(receipt_path)
    capture_headers = {
        **auth,
        "Idempotency-Key": KEY,
        "Content-Type": "application/json",
    }
    with TestClient(app_factory) as client:
        captured = client.post("/v1/episodic", content=RAW_CAPTURE, headers=capture_headers)
        assert captured.status_code == 202, captured.text
        looked_up = client.post(
            "/v1/idempotency/receipts/lookup",
            json=_lookup_body(),
            headers=auth,
        )
    assert looked_up.status_code == 200, looked_up.text
    payload = looked_up.json()
    assert payload["status"] == "found"
    assert payload["object_id"] == captured.json()["object_id"]
    assert payload["namespace"] == NAMESPACE
    assert payload["response_sha256"] == hashlib.sha256(captured.content).hexdigest()
