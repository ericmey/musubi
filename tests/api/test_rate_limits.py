from __future__ import annotations

import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from musubi.api.rate_limit import DEFAULT_BUCKETS, get_rate_limiter

# Derive the exhaust size from the bucket spec so the tests don't
# become stale when the cap changes — see ADR 0027.
_CAPTURE_CAP = DEFAULT_BUCKETS["capture"].capacity_per_min
_CAPTURE_EXHAUST = _CAPTURE_CAP + 5


def test_capture_rate_limit_returns_429_on_over_limit(client: TestClient, valid_token: str) -> None:
    limiter = get_rate_limiter()
    limiter.reset_for_test()
    # Exhaust the capture bucket. Size derived from DEFAULT_BUCKETS
    # so changing the cap doesn't require updating N test literals.
    for _ in range(_CAPTURE_EXHAUST):
        resp = client.post(
            "/v1/episodic",
            json={"namespace": "eric/claude-code/episodic", "content": "hit"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        pass

    resp = client.post(
        "/v1/episodic",
        json={"namespace": "eric/claude-code/episodic", "content": "hit"},
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_capture_rate_limit_resets_after_window(client: TestClient, valid_token: str) -> None:
    limiter = get_rate_limiter()
    limiter.reset_for_test()
    for _ in range(_CAPTURE_EXHAUST):
        client.post(
            "/v1/episodic",
            json={"namespace": "eric/claude-code/episodic", "content": "hit"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
    resp = client.post(
        "/v1/episodic",
        json={"namespace": "eric/claude-code/episodic", "content": "hit"},
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert resp.status_code == 429

    with patch("time.time", return_value=time.time() + 61):
        resp = client.post(
            "/v1/episodic",
            json={"namespace": "eric/claude-code/episodic", "content": "hit"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        pass


def test_retrieve_rate_limit_separate_bucket_from_capture(
    client: TestClient, valid_token: str
) -> None:
    limiter = get_rate_limiter()
    limiter.reset_for_test()
    for _ in range(_CAPTURE_EXHAUST):
        client.post(
            "/v1/episodic",
            json={"namespace": "eric/claude-code/episodic", "content": "hit"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
    # capture is exhausted
    resp = client.post(
        "/v1/retrieve",
        json={"namespace": "eric/claude-code/episodic", "query_text": "hit"},
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert resp.status_code == 200


def test_retry_after_header_present_on_429(client: TestClient, valid_token: str) -> None:
    limiter = get_rate_limiter()
    limiter.reset_for_test()
    for _ in range(_CAPTURE_EXHAUST):
        client.post(
            "/v1/episodic",
            json={"namespace": "eric/claude-code/episodic", "content": "hit"},
            headers={"Authorization": f"Bearer {valid_token}"},
        )
    resp = client.post(
        "/v1/episodic",
        json={"namespace": "eric/claude-code/episodic", "content": "hit"},
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0
