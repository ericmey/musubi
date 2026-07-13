"""SEC-002 (C1) P0 — idempotency replay bypasses authentication.

Discoverer: Eric. Source-confirmed by Yua (router). Red tests: Aoi.

The idempotency cache runs BEFORE authentication (app.py middleware order) and binds only
(Idempotency-Key header, body hash) — nothing about the caller (idempotency.py:59
lookup(key, body)). So a cached 2xx is replayed to ANY caller presenting the same key +
body: no bearer, a bad bearer, or a DIFFERENT TENANT's bearer.

These tests assert the SECURE behaviour and are marked xfail(strict=True): they FAIL today
(documenting the hole) and will turn GREEN when the fix lands — an unexpected pass then
signals the fix, not a broken test.

No live/sensitive content — all fixtures are synthetic.

Run:  pytest tests/api/test_sec002_idempotency_auth.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from musubi.settings import Settings
from tests.api.conftest import mint_token

CAPTURE = "/v1/episodic"
IDEM = "Idempotency-Key"


def _capture_body(
    ns: str = "eric/claude-code/episodic", content: str = "sec002 probe"
) -> dict[str, Any]:
    return {"namespace": ns, "content": content, "tags": ["kind:episode"], "importance": 3}


def _prime(client: TestClient, token: str, key: str, body: dict[str, Any]) -> None:
    """Legitimately populate the idempotency cache with an authenticated write."""
    r = client.post(CAPTURE, json=body, headers={"Authorization": f"Bearer {token}", IDEM: key})
    assert r.status_code in (200, 201, 202), f"priming write failed: {r.status_code} {r.text[:200]}"


@pytest.mark.xfail(
    strict=True,
    reason="SEC-002: replay served with NO bearer — deferred; closed by slice-idempotency-phase-b (PR #404, Issue #407)",
)
def test_no_bearer_must_not_replay(client: TestClient, valid_token: str) -> None:
    key, body = "sec002-nobearer", _capture_body()
    _prime(client, valid_token, key, body)
    # same key + same body, NO Authorization header
    r = client.post(CAPTURE, json=body, headers={IDEM: key})
    # SECURE expectation: unauthenticated request is rejected, never served a cached 2xx
    assert r.status_code == 401, (
        f"unauthenticated replay returned {r.status_code} "
        f"(replay={r.headers.get('X-Idempotent-Replay')}) — auth was bypassed"
    )


@pytest.mark.xfail(
    strict=True,
    reason="SEC-002: replay served to INVALID bearer — deferred; closed by slice-idempotency-phase-b (PR #404, Issue #407)",
)
def test_invalid_bearer_must_not_replay(client: TestClient, valid_token: str) -> None:
    key, body = "sec002-badbearer", _capture_body()
    _prime(client, valid_token, key, body)
    r = client.post(
        CAPTURE, json=body, headers={"Authorization": "Bearer not-a-real-token", IDEM: key}
    )
    assert r.status_code == 401, (
        f"invalid-bearer replay returned {r.status_code} — auth was bypassed"
    )


@pytest.mark.xfail(
    strict=True,
    reason="SEC-002: one tenant replays another tenant's write — deferred; closed by slice-idempotency-phase-b (PR #404, Issue #407)",
)
def test_cross_tenant_must_not_replay(
    client: TestClient, api_settings: Settings, valid_token: str
) -> None:
    key, body = "sec002-crosstenant", _capture_body()
    _prime(client, valid_token, key, body)  # tenant A (eric/claude-code) writes
    # tenant B: a VALID token for a DIFFERENT presence with no access to A's namespace
    tenant_b = mint_token(
        api_settings, scopes=["mallory/evil/episodic:rw"], presence="mallory/evil"
    )
    prime = client.post(
        CAPTURE, json=body, headers={"Authorization": f"Bearer {valid_token}", IDEM: key}
    )
    leaked_id = (
        prime.json().get("object_id")
        if prime.headers.get("content-type", "").startswith("application/json")
        else None
    )
    r = client.post(CAPTURE, json=body, headers={"Authorization": f"Bearer {tenant_b}", IDEM: key})
    # SECURE (Yua): cross-tenant replay must be REFUSED (403), and must not DISCLOSE A's
    # cached body — absence of the replay header alone is not enough; the object_id and
    # body of A's write must not reach B.
    assert r.status_code == 403, (
        f"cross-tenant replay returned {r.status_code} — B was authorized on A's write"
    )
    assert r.headers.get("X-Idempotent-Replay") != "true"
    if leaked_id is not None:
        assert leaked_id not in r.text, "tenant A's object_id leaked to tenant B"


@pytest.mark.skip(
    reason="SEC-002 collision probe NOT YET VALID — see note. Do not cite as evidence."
)
def test_same_key_body_must_not_collide_across_routes(
    client: TestClient, api_settings: Settings, valid_token: str
) -> None:
    """PLACEHOLDER — my first collision probe was WRONG and I will not fake it.

    I tried two capture bodies differing only by `namespace`. Because the BODIES differ,
    the cache correctly returns 409 Conflict (same key + different body) — that is the
    conflict path WORKING, not a collision. It proves nothing about cross-route/namespace
    key collision.

    A valid collision probe needs the SAME key with a BYTE-IDENTICAL body hash sent to TWO
    DIFFERENT routes/namespaces (e.g. /v1/episodic vs a second write route), so the only
    difference is the route the cache does not bind. Requires a second write endpoint with
    a compatible body shape. Left skipped and documented rather than reported as a
    reproduced hole. (Yua's standard: do not claim a vulnerability that does not
    reproduce.)"""
    pytest.skip("needs a second write route with an identical-hash body")


def test_owner_can_replay_its_own_write(client: TestClient, valid_token: str) -> None:
    """The legitimate case MUST keep working: the original authenticated subject replays.

    NOT xfail — idempotency for the rightful owner is the feature, and the fix must
    preserve it. If this ever fails, the fix over-corrected.
    """
    key, body = "sec002-owner", _capture_body()
    _prime(client, valid_token, key, body)
    r = client.post(
        CAPTURE, json=body, headers={"Authorization": f"Bearer {valid_token}", IDEM: key}
    )
    assert r.status_code in (200, 201, 202)
    assert r.headers.get("X-Idempotent-Replay") == "true", (
        "the original authenticated subject must still get its idempotent replay"
    )
