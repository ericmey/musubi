"""IDEM-005 typed, provably pre-mutation episodic content rejection."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from musubi.api.dependencies import get_episodic_plane
from musubi.api.idempotency import IdempotencyLeaseCache, get_idempotency_lease_cache

NAMESPACE = "eric/claude-code/episodic"


class _RecordingPlane:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def create(self, memory: Any, **_kwargs: object) -> Any:
        self.calls.append(memory)
        return memory


class _NoAcquireCache(IdempotencyLeaseCache):
    def acquire(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("oversize rejection reached idempotency acquisition")


def _headers(token: str, *, durable: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "idem005-boundary-proof",
    }
    if durable:
        headers["Idempotency-Receipt"] = "durable"
    return headers


@pytest.mark.parametrize("content", ["a" * 32_768, "é" * 16_384])
def test_exact_32768_utf8_bytes_reaches_plane(
    app_factory: Any,
    valid_token: str,
    content: str,
) -> None:
    plane = _RecordingPlane()
    app_factory.dependency_overrides[get_episodic_plane] = lambda: plane

    with TestClient(app_factory) as client:
        response = client.post(
            "/v1/episodic",
            headers=_headers(valid_token),
            json={"namespace": NAMESPACE, "content": content},
        )

    assert response.status_code == 202, response.text
    assert len(plane.calls) == 1
    assert len(plane.calls[0].content.encode("utf-8")) == 32_768


@pytest.mark.parametrize("content", ["a" * 32_769, "é" * 16_384 + "a"])
def test_32769_utf8_bytes_is_typed_and_never_reaches_idempotency_or_plane(
    app_factory: Any,
    valid_token: str,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    plane = _RecordingPlane()
    app_factory.dependency_overrides[get_episodic_plane] = lambda: plane
    app_factory.dependency_overrides[get_idempotency_lease_cache] = _NoAcquireCache

    def _receipt_store_must_not_resolve(_request: object) -> object:
        raise AssertionError("oversize rejection reached the durable receipt store")

    monkeypatch.setattr(
        "musubi.api.idempotency_dependency.get_idempotency_receipt_store",
        _receipt_store_must_not_resolve,
    )

    with TestClient(app_factory) as client:
        response = client.post(
            "/v1/episodic",
            headers=_headers(valid_token, durable=True),
            json={"namespace": NAMESPACE, "content": content},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "CONTENT_TOO_LARGE",
            "detail": "episodic content is 32769 UTF-8 bytes; the limit is 32768",
            "hint": "use the artifact plane for content larger than 32 KiB",
        }
    }
    assert plane.calls == []


def test_batch_oversize_preflight_is_all_or_none_before_idempotency_and_plane(
    app_factory: Any,
    valid_token: str,
) -> None:
    plane = _RecordingPlane()
    app_factory.dependency_overrides[get_episodic_plane] = lambda: plane
    app_factory.dependency_overrides[get_idempotency_lease_cache] = _NoAcquireCache

    with TestClient(app_factory) as client:
        response = client.post(
            "/v1/episodic/batch",
            headers=_headers(valid_token),
            json={
                "namespace": NAMESPACE,
                "items": [
                    {"content": "would-have-been-item-one"},
                    {"content": "a" * 32_769},
                ],
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTENT_TOO_LARGE"
    assert plane.calls == []


def test_namespace_authorization_precedes_oversize_verdict(
    app_factory: Any,
    out_of_scope_token: str,
) -> None:
    with TestClient(app_factory) as client:
        response = client.post(
            "/v1/episodic",
            headers=_headers(out_of_scope_token),
            json={"namespace": NAMESPACE, "content": "a" * 32_769},
        )

    assert response.status_code == 403
    assert "32769" not in response.text
    assert "CONTENT_TOO_LARGE" not in response.text
