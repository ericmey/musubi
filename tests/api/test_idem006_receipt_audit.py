"""IDEM-006 exact, read-only two-seat receipt audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from fastapi.testclient import TestClient

from musubi.api.idempotency import CompletedResponse
from musubi.api.idempotency_receipts import DurableReceiptStore
from musubi.settings import Settings

ISSUER = "https://auth.example.test"
NAMESPACE = "yua/command-chair/episodic"
OPERATION = "capture_episodic.bucket=capture"
KEY = "idem006-exact-known-key"
DIGEST = bytes.fromhex("ab" * 32)
TARGET = (
    ISSUER,
    "yua-codex",
    "yua/command-chair",
    "POST",
    OPERATION,
    NAMESPACE,
    KEY,
)
RESPONSE = CompletedResponse(
    status=202,
    raw_headers=((b"content-type", b"application/json"),),
    body=b'{"object_id":"ep-audit-1","state":"provisional"}',
)


def _token(
    settings: Settings,
    *,
    scopes: list[str],
    subject: str = "aoi-operator",
    presence: str = "aoi/command-chair",
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": ISSUER,
            "sub": subject,
            "aud": "musubi",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "scope": " ".join(scopes),
            "presence": presence,
        },
        settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )


def _body(*, digest: bytes = DIGEST, target: tuple[str, ...] = TARGET) -> dict[str, str]:
    return {
        "target_issuer": target[0],
        "target_subject": target[1],
        "target_presence": target[2],
        "namespace": target[5],
        "method": "POST",
        "operation_id": target[4],
        "idempotency_key": target[6],
        "request_digest": digest.hex(),
    }


def _combined_token(settings: Settings, **identity: str) -> str:
    return _token(settings, scopes=["operator", "**:r"], **identity)


def _store(path: Path) -> DurableReceiptStore:
    store = DurableReceiptStore(path)
    store.store(
        identity=TARGET,
        digest=DIGEST,
        response=RESPONSE,
        namespace=NAMESPACE,
        operation=OPERATION,
    )
    return store


class _ExplodingStore:
    def lookup(self, **_kwargs: object) -> object:
        raise AssertionError("receipt storage was touched before both authorization gates")


def test_audit_requires_operator_and_namespace_read_before_storage(
    app_factory: object,
    api_settings: Settings,
) -> None:
    app_factory.state.idempotency_receipt_store = _ExplodingStore()  # type: ignore[attr-defined]
    tokens = (
        (None, 401),
        (_token(api_settings, scopes=["operator"]), 403),
        (_token(api_settings, scopes=["**:r"]), 403),
    )
    with TestClient(app_factory) as client:  # type: ignore[arg-type]
        for token, expected in tokens:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            response = client.post("/v1/idempotency/receipts/audit", json=_body(), headers=headers)
            assert response.status_code == expected


def test_cross_principal_exact_audit_returns_found_with_server_observer(
    app_factory: object,
    api_settings: Settings,
    tmp_path: Path,
) -> None:
    app_factory.state.idempotency_receipt_store = _store(tmp_path / "receipts.sqlite")  # type: ignore[attr-defined]
    token = _combined_token(api_settings)
    with TestClient(app_factory) as client:  # type: ignore[arg-type]
        response = client.post(
            "/v1/idempotency/receipts/audit",
            json=_body(),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "found"
    assert payload["observer_attestation"] == "server_attested"
    assert payload["observer_issuer"] == ISSUER
    assert payload["observer_subject"] == "aoi-operator"
    assert payload["observer_presence"] == "aoi/command-chair"
    assert payload["observer_effective_scopes"] == ["operator", "**:r"]
    assert payload["namespace"] == NAMESPACE
    assert payload["operation_id"] == OPERATION
    assert payload["request_digest"] == DIGEST.hex()
    assert payload["object_id"] == "ep-audit-1"
    assert payload["response_status"] == 202
    assert payload["receipt_committed_at"].endswith("Z")
    assert len(payload["target_identity_hash"]) == 64
    assert KEY not in response.text


def test_cross_principal_wrong_digest_collapses_conflict_to_absent(
    app_factory: object,
    api_settings: Settings,
    tmp_path: Path,
) -> None:
    app_factory.state.idempotency_receipt_store = _store(tmp_path / "receipts.sqlite")  # type: ignore[attr-defined]
    token = _combined_token(api_settings)
    with TestClient(app_factory) as client:  # type: ignore[arg-type]
        response = client.post(
            "/v1/idempotency/receipts/audit",
            json=_body(digest=bytes.fromhex("cd" * 32)),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "absent"
    assert response.json()["object_id"] is None
    assert KEY not in response.text


def test_owning_principal_keeps_conflict_fidelity(
    app_factory: object,
    api_settings: Settings,
    tmp_path: Path,
) -> None:
    app_factory.state.idempotency_receipt_store = _store(tmp_path / "receipts.sqlite")  # type: ignore[attr-defined]
    token = _combined_token(
        api_settings,
        subject="yua-codex",
        presence="yua/command-chair",
    )
    with TestClient(app_factory) as client:  # type: ignore[arg-type]
        response = client.post(
            "/v1/idempotency/receipts/audit",
            json=_body(digest=bytes.fromhex("cd" * 32)),
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "conflict"
