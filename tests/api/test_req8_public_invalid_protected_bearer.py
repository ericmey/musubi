"""REQ-8 — public-absent vs presented-invalid vs protected-absent bearer semantics.

Yua req 8 (21:18): "public absent-bearer stays public, presented-invalid bearer fails,
protected absent fails."

Three distinct contracts, checked against real routes on a real create_app:

  1. PUBLIC route + ABSENT bearer   -> stays public (200). Control.
  2. ANY route + PRESENTED-INVALID bearer -> 401. A client that PRESENTS a bearer is asserting
     an identity; a bad token must be rejected, never silently ignored and treated as anonymous
     (the client believes it is authenticated). Today a public route IGNORES the header -> 200.
  3. PROTECTED route + ABSENT bearer -> 401. Control.

Observed today (documented in the reds):
  public /v1/ops/health   : absent -> 200 · invalid-bearer -> 200 (the hole)
  protected /v1/episodic  : absent -> 401 · invalid-bearer -> 401 (already correct)

`xfail(strict=True)` on the hole (case 2 on the public route); plain controls for the rest.
Tests/docs only, no src.

    uv run pytest tests/api/test_req8_public_invalid_protected_bearer.py -v
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

PUBLIC = "/v1/ops/health"  # no auth dependency
PROTECTED = "/v1/episodic"  # require_auth route dependency
_PROTECTED_QS = {"namespace": "eric/claude-code/episodic", "query": "x"}
_BAD = {"Authorization": "Bearer not-a-real-token"}


def test_public_absent_bearer_stays_public(client: TestClient) -> None:
    """Control: a public route with NO bearer is served (200)."""
    r = client.get(PUBLIC)
    assert r.status_code == 200, (
        f"public route without a bearer must stay public, got {r.status_code}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="REQ-8: a presented-invalid bearer is ignored on public routes — fix pending",
)
def test_public_presented_invalid_bearer_must_fail(client: TestClient) -> None:
    """The hole: presenting an INVALID bearer to a public route is silently ignored (200 today).
    A presented-but-invalid credential must be rejected (401), not treated as anonymous."""
    r = client.get(PUBLIC, headers=_BAD)
    assert r.status_code == 401, (
        f"public route accepted a presented-invalid bearer ({r.status_code}) — an invalid token "
        f"must be rejected, never silently downgraded to anonymous"
    )


def test_protected_absent_bearer_must_fail(client: TestClient) -> None:
    """Control: a protected route with NO bearer is 401."""
    r = client.get(PROTECTED, params=_PROTECTED_QS)
    assert r.status_code == 401, (
        f"protected route without a bearer must be 401, got {r.status_code}"
    )


def test_protected_presented_invalid_bearer_must_fail(client: TestClient) -> None:
    """Control: a protected route with an INVALID bearer is 401 (already correct)."""
    r = client.get(PROTECTED, params=_PROTECTED_QS, headers=_BAD)
    assert r.status_code == 401, (
        f"protected route with an invalid bearer must be 401, got {r.status_code}"
    )
