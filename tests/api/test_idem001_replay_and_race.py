"""IDEM-001 P0 — idempotency identity omits the endpoint, and has no in-flight lease.

Discoverer: Aoi (reading app.py:240 + idempotency.py:66). Router-confirmed by Yua.
Consolidated with SEC-002/003/004 as the auth-boundary red-contract (ADR D3/D6, req 9 + 3).

TWO defects, both proven here against the REAL code path:

  (A) CROSS-ENDPOINT REPLAY (req 9).  The write-side idempotency middleware
      (`app.py:_wrapped_call`) calls `cache.lookup(idem_key, body_for_hash)` with NO route or
      operation in the identity, and it runs BEFORE the route handler validates the body. So
      the SAME `Idempotency-Key` + SAME body sent to a DIFFERENT write endpoint is served the
      FIRST endpoint's cached 2xx with `X-Idempotent-Replay: true` — a single-capture response
      replayed onto the batch endpoint, across operations that share nothing but the key.

  (B) NO IN-FLIGHT LEASE / THE RACE (req 3).  `IdempotencyCache` exposes `lookup` then `store`
      with nothing in between: two concurrent callers with the same key BOTH get "miss" before
      either stores, so BOTH execute the mutation. There is no acquire/lease primitive — which
      is exactly what the ADR-D3 split pipeline's `acquire` step is for.

The race is proven at the CACHE UNIT level on purpose: it is deterministic and self-proving,
not a flaky threaded integration test that may or may not interleave. It demonstrates the
missing primitive directly.

`xfail(strict=True)` = asserts the SECURE behaviour, FAILS today, flips to XPASS→fail when the
fix lands (signalling the fix, not a broken test). Plain asserts = controls / today-reality
proofs that must always hold. All content synthetic; no live memory.

    uv run pytest tests/api/test_idem001_replay_and_race.py -v
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient

from musubi.api.idempotency import IdempotencyCache

CAPTURE = "/v1/episodic"
BATCH = "/v1/episodic/batch"  # a DIFFERENT operation_id, same key space today
IDEM = "Idempotency-Key"
REPLAY = "X-Idempotent-Replay"


def _capture_body(
    ns: str = "eric/claude-code/episodic", content: str = "idem001 probe"
) -> dict[str, Any]:
    return {"namespace": ns, "content": content, "tags": ["kind:episode"], "importance": 3}


def _prime(client: TestClient, token: str, key: str, body: dict[str, Any]) -> None:
    """Legitimately populate the idempotency cache with an authenticated capture."""
    r = client.post(CAPTURE, json=body, headers={"Authorization": f"Bearer {token}", IDEM: key})
    assert r.status_code in (200, 201, 202), (
        f"priming capture failed: {r.status_code} {r.text[:200]}"
    )
    # the priming write itself must NOT already be a replay — proves the cache started empty
    assert r.headers.get(REPLAY) != "true", "priming write was itself a replay — fixture is dirty"


# --------------------------------------------------------------------------- #
# (A) cross-endpoint replay
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="IDEM-001(A): key+body replays across DIFFERENT endpoints — deferred; closed by slice-idempotency-phase-b (PR #404, Issue #407)",
)
def test_same_key_body_must_not_replay_across_endpoints(
    client: TestClient, valid_token: str
) -> None:
    key, body = "idem001-crossroute", _capture_body()
    _prime(client, valid_token, key, body)
    # SAME key + SAME body, DIFFERENT endpoint (batch). Auth is present and valid — this is
    # purely about identity scope, not authn (that is SEC-002).
    r = client.post(BATCH, json=body, headers={"Authorization": f"Bearer {valid_token}", IDEM: key})
    # SECURE: the batch endpoint must NOT be served the capture endpoint's cached response.
    # Identity must include the route/operation, so this is a miss (fresh handling) or a
    # validation error on the batch schema — never a cross-endpoint replay.
    assert r.headers.get(REPLAY) != "true", (
        f"cross-endpoint replay: {BATCH} was served {CAPTURE}'s cached response "
        f"(status={r.status_code}, {REPLAY}={r.headers.get(REPLAY)}) — identity omits the endpoint"
    )


def test_replay_on_same_endpoint_still_works(client: TestClient, valid_token: str) -> None:
    """Feature preservation: same key+body on the SAME endpoint must still replay (that is the
    whole point of idempotency). NOT xfail — the fix must scope identity by endpoint, not kill
    replay. This control must stay green before AND after the fix."""
    key, body = "idem001-sameroute", _capture_body()
    _prime(client, valid_token, key, body)
    r = client.post(
        CAPTURE, json=body, headers={"Authorization": f"Bearer {valid_token}", IDEM: key}
    )
    assert r.status_code in (200, 201, 202), f"same-endpoint replay failed: {r.status_code}"
    assert r.headers.get(REPLAY) == "true", (
        "same key+body on the SAME endpoint must replay — idempotency's core feature"
    )


# --------------------------------------------------------------------------- #
# (B) the race — no in-flight lease. Deterministic, cache-unit level.
# --------------------------------------------------------------------------- #


def test_race_window_exists_two_concurrent_callers_both_miss() -> None:
    """TODAY-REALITY PROOF (not xfail): two callers looking up the same key BEFORE either
    stores BOTH receive "miss", so BOTH proceed to execute the mutation. This is the race
    window, proven deterministically — no lease primitive exists to close it."""
    cache = IdempotencyCache()
    body = _capture_body()
    first, _, _ = cache.lookup("idem001-race", body)
    second, _, _ = cache.lookup("idem001-race", body)  # concurrent second caller, pre-store
    assert first == "miss" and second == "miss", (
        f"expected the race window (miss, miss); got ({first}, {second}) — "
        f"if this changed, a lease may now exist and the xfail below should XPASS"
    )


@pytest.mark.xfail(
    strict=True,
    reason="IDEM-001(B): no in-flight lease — second concurrent caller gets a free miss — deferred; closed by slice-idempotency-phase-b (PR #404, Issue #407)",
)
def test_second_concurrent_caller_must_not_get_free_miss() -> None:
    """SECURE CONTRACT: once a caller has claimed a key, a concurrent second caller with the
    same key must be told it is in-flight (so it waits/replays/409s), NOT handed a plain "miss"
    that lets it execute the same mutation a second time. The ADR-D3 `acquire` lease is the
    fix. Fails today (there is no acquire; the second caller gets "miss")."""
    cache = IdempotencyCache()
    body = _capture_body()
    claimed, _, _ = cache.lookup("idem001-lease", body)  # first caller claims
    assert claimed == "miss", "first caller should see a miss (the key is unused)"
    second, _, _ = cache.lookup("idem001-lease", body)  # concurrent second caller
    assert second != "miss", (
        "no in-flight lease: a concurrent second caller for a claimed key also got 'miss' — "
        "both callers execute the mutation (double write)"
    )


def test_conflict_still_detected_same_key_different_body() -> None:
    """Control: same key + DIFFERENT body must be a conflict, not a silent miss or a wrong-body
    replay. NOT xfail — must hold before and after the fix."""
    cache = IdempotencyCache()
    cache.store(
        "idem001-conflict",
        _capture_body(content="original"),
        response_status=202,
        response_body={"object_id": "x"},
    )
    status, _, _ = cache.lookup("idem001-conflict", _capture_body(content="DIFFERENT"))
    assert status == "conflict", f"same key + different body must be 'conflict', got {status!r}"
