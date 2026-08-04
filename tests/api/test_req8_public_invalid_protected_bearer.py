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

from typing import Any

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


def test_public_presented_invalid_bearer_must_fail(client: TestClient) -> None:
    """CLOSED (Issue #413): presenting an INVALID bearer to a public route is 401.

    Was strict-xfail while the hole was open — a public route ignored the header and
    returned 200, byte-identical to the anonymous response, so the client believed it
    was authenticated and nothing said otherwise. The marker was removed only after
    this test XPASSed for the intended reason: `PresentedBearerGuard` serving a typed
    401 on `GET /v1/ops/health` with a bad bearer.
    """
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


# ---------------------------------------------------------------------------
# Issue #413 acceptance beyond the original three contracts. The pre-code survey
# established that exactly five routes ignored a presented-invalid bearer:
# /v1/ops/health, /v1/ops/status, /v1/ops/metrics, /v1/docs, /v1/openapi.json.
# Every other no-route-dependency path already authenticated in-handler.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from musubi.settings import Settings  # noqa: E402
from tests.api.conftest import mint_token  # noqa: E402

PUBLIC_SURFACE = [
    "/v1/ops/health",
    "/v1/ops/status",
    "/v1/ops/metrics",
    "/v1/docs",
    "/v1/openapi.json",
]


def test_every_public_route_rejects_a_presented_invalid_bearer(client: TestClient) -> None:
    """All five surveyed public routes, not just the health probe."""
    for path in PUBLIC_SURFACE:
        assert client.get(path, headers=_BAD).status_code == 401, (
            f"{path} accepted a presented-invalid bearer"
        )


def test_every_public_route_still_serves_without_a_bearer(client: TestClient) -> None:
    """The regression that would matter operationally: absent stays public.

    Every repo-owned health probe (docker-compose healthcheck, smoke-health.sh,
    ansible group_vars) sends NO Authorization header, so this is the assertion
    standing between this change and a fleet-wide false outage.
    """
    for path in PUBLIC_SURFACE:
        assert client.get(path).status_code == 200, f"{path} stopped serving anonymously"


def test_public_route_accepts_a_VALID_bearer(client: TestClient, api_settings: Settings) -> None:
    """A valid credential on a public route is still served — we reject invalid
    presentation, not presentation itself."""
    good = {"Authorization": f"Bearer {mint_token(api_settings)}"}
    for path in PUBLIC_SURFACE:
        assert client.get(path, headers=good).status_code == 200, f"{path} rejected a VALID bearer"


def test_expired_bearer_is_rejected_on_a_public_route(
    client: TestClient, api_settings: Settings
) -> None:
    """The realistic failure: a stale credential, not a garbage string.

    This is the case that flips deploy/smoke from pass to fail, and it is the
    intended behaviour — a smoke check running on an expired token must not
    silently report health.
    """
    stale = mint_token(api_settings, expires_delta=timedelta(hours=-1))
    assert client.get(PUBLIC, headers={"Authorization": f"Bearer {stale}"}).status_code == 401


def test_empty_bearer_value_is_rejected_not_treated_as_absent(client: TestClient) -> None:
    """`Authorization: Bearer ` presents a credential and supplies none.

    The pre-existing `_bearer_token` helper collapses this to None — correct when
    REQUIRING auth, wrong when DETECTING presentation. Building REQ-8 on that helper
    would have let an empty bearer slip through the hole it exists to close.
    """
    assert client.get(PUBLIC, headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get(PUBLIC, headers={"Authorization": "Bearer"}).status_code == 401


def test_non_bearer_authorization_scheme_passes_through(client: TestClient) -> None:
    """Deliberate scope boundary: REQ-8 speaks about a presented BEARER.

    Rejecting `Basic` is a policy REQ-8 does not state and no repo caller exercises,
    so it is left alone. Documented as a decision, not an oversight — widen it in a
    slice that says so.
    """
    assert client.get(PUBLIC, headers={"Authorization": "Basic dXNlcjpwdw=="}).status_code == 200


def test_rejection_uses_the_canonical_typed_error_envelope(client: TestClient) -> None:
    """A 401 here must look like every other Musubi error, not a bespoke body."""
    r = client.get(PUBLIC, headers=_BAD)
    assert r.status_code == 401
    body = r.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == "UNAUTHORIZED", body
    assert body["error"]["detail"], "detail must not be empty"


# ---------------------------------------------------------------------------
# Single-validation regression. Found by Yua in terminal review of the first
# revision: the guard validated at the edge and the handler validated the SAME
# token again — a valid GET /v1/namespaces returned 200 with validate_token
# call_count=2. tokens.py has no JWKS cache, so under RS256 that is two
# synchronous JWKS fetches per protected request.
# ---------------------------------------------------------------------------


def test_valid_bearer_on_protected_route_validates_exactly_once(
    client: TestClient, api_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cryptographic validation per request, and the route still authorizes."""
    from musubi.auth.tokens import validate_token as real_validate_token

    calls: list[str] = []

    def counting(token: str, **kw: Any) -> Any:
        calls.append(token)
        return real_validate_token(token, **kw)

    # String target: `validate_token` is imported into musubi.auth.middleware and is
    # not in its __all__, so a direct attribute reference trips no_implicit_reexport.
    monkeypatch.setattr("musubi.auth.middleware.validate_token", counting)

    # Default mint_token scope is exactly `eric/claude-code/episodic:r`, the namespace
    # in _PROTECTED_QS — so this request genuinely SUCCEEDS.
    token = mint_token(api_settings)
    r = client.get(PROTECTED, params=_PROTECTED_QS, headers={"Authorization": f"Bearer {token}"})

    # EXACT 200, not "not 401". Yua on the first revision: `!= 401` passes on 403 or
    # 500, so it never proved an authenticated request completed — and it WAS passing
    # on a 403, because the scope string used here originally (`eric/*:rw`) does not
    # grant that namespace. A validation count only means something for a request that
    # actually reached the handler.
    assert r.status_code == 200, f"valid in-scope bearer did not succeed: {r.status_code} {r.text}"
    assert len(calls) == 1, (
        f"expected exactly 1 cryptographic validation, got {len(calls)} — the edge "
        f"guard and the handler are both validating the same token"
    )


def test_reused_context_still_enforces_the_route_requirement(
    client: TestClient, api_settings: Settings
) -> None:
    """The seam caches VALIDATION only — never authorization.

    A token that is cryptographically valid but scoped to another tenant must still
    be refused by the protected route. If the guard's context short-circuited the
    AuthRequirement, this would wrongly succeed.
    """
    wrong = mint_token(api_settings, scopes=["mallory/evil/episodic:rw"])
    r = client.get(PROTECTED, params=_PROTECTED_QS, headers={"Authorization": f"Bearer {wrong}"})

    # EXACT canonical scope denial, not "401 or 403". Yua on the first revision: a 401
    # would mean the token was rejected CRYPTOGRAPHICALLY, which proves nothing about
    # _check_requirement having run. Only a 403 carrying the scope error proves the
    # request got PAST validation and was refused by AUTHORIZATION — which is the whole
    # claim of the cache seam.
    assert r.status_code == 403, (
        f"expected the canonical scope denial, got {r.status_code}: {r.text}"
    )
    error = r.json()["error"]
    assert error["code"] == "FORBIDDEN", error
    assert (
        error["detail"] == "namespace 'eric/claude-code/episodic' not in token scope for 'r' access"
    ), error


def test_401_hint_is_truthful_on_a_protected_route(client: TestClient) -> None:
    """The first revision told protected callers to 'omit it to call a public route'.
    Omitting on a protected route is still a 401, so that hint was false."""
    hint = client.get(PROTECTED, params=_PROTECTED_QS, headers=_BAD).json()["error"]["hint"]
    assert "omit it to call a public route" not in hint, f"stale misleading hint: {hint}"
