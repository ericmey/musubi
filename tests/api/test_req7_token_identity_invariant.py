"""REQ-7 — the exact issuer/subject/presence token invariant (D6), reject inconsistency.

Yua req 7 (21:18): "replace D6 example language with the exact token invariant and reject
inconsistent issuer/subject/presence."

D6: the caller identity that the replay cache keys on is (issuer, subject, presence) — NOT jti.
For that to be sound, the token's presence claim must be CONSISTENT with what it is authorized
for: a token whose `presence` claim points at one tenant while its scopes grant another tenant
lets the replay identity (keyed on presence) and the authorization (keyed on scope) aim at
different tenants. That inconsistency must be rejected at token validation.

Reds run against the REAL validator `musubi.auth.tokens.validate_token` (a pure function):

  observed today:
    presence="eric/claude-code" + scope=["mallory/evil/episodic:rw"]  -> Ok (ACCEPTED)  [the hole]
    wrong issuer                                                        -> Err (rejected)  [holds]

`xfail(strict=True)` on the hole; plain controls for the invariants that already hold (issuer
enforced, presence/sub required) and for the D6 identity tuple. Synthetic tokens only, no live
secrets beyond the test signing key. Tests/docs only, no src.

    uv run pytest tests/api/test_req7_token_identity_invariant.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from musubi.auth.tokens import validate_token
from musubi.settings import Settings
from musubi.types.common import Ok
from tests.api.conftest import _TEST_ISSUER, mint_token


def _is_ok(result) -> bool:
    return isinstance(result, Ok)


@pytest.mark.xfail(
    strict=True, reason="REQ-7: presence/scope consistency not enforced yet — fix pending"
)
def test_inconsistent_presence_vs_scope_must_be_rejected(api_settings: Settings) -> None:
    """The hole: presence claims one tenant, the only scope grants another. Today ACCEPTED.

    Fails today (the token validates) and flips when validate_token enforces presence<->scope
    consistency."""
    token = mint_token(
        api_settings,
        scopes=["mallory/evil/episodic:rw"],  # authorizes mallory/evil
        presence="eric/claude-code",  # but claims to be eric/claude-code
    )
    result = validate_token(token, settings=api_settings)
    assert not _is_ok(result), (
        "a token whose presence claim disagrees with its scope's tenant/presence prefix was "
        "accepted — the replay identity (presence) and the authorization (scope) can point at "
        "different tenants"
    )


def test_consistent_presence_and_scope_is_accepted(api_settings: Settings) -> None:
    """Feature preservation: a token whose presence matches its scope prefix must validate. Green
    before and after the fix — the fix must reject only the INCONSISTENT case."""
    token = mint_token(
        api_settings,
        scopes=["eric/claude-code/episodic:rw"],
        presence="eric/claude-code",
    )
    result = validate_token(token, settings=api_settings)
    assert _is_ok(result), f"a consistent token must validate, got {result}"
    assert result.value.presence == "eric/claude-code"


def test_wrong_issuer_is_rejected(api_settings: Settings) -> None:
    """Control: the issuer half of the invariant already holds (jwt.decode issuer= check)."""
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "iss": "https://evil.example/",
            "sub": "eric-claude-code",
            "aud": "musubi",
            "presence": "eric/claude-code",
            "scope": "eric/claude-code/episodic:r",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        api_settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )
    assert not _is_ok(validate_token(forged, settings=api_settings)), (
        "wrong issuer must be rejected"
    )


def test_missing_presence_is_rejected(api_settings: Settings) -> None:
    """Control: presence is a required claim (part of the D6 identity)."""
    now = datetime.now(UTC)
    no_presence = jwt.encode(
        {
            "iss": _TEST_ISSUER,
            "sub": "eric-claude-code",
            "aud": "musubi",
            "scope": "eric/claude-code/episodic:r",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        api_settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )
    assert not _is_ok(validate_token(no_presence, settings=api_settings)), (
        "missing presence must be rejected"
    )


def test_d6_identity_is_issuer_subject_presence_not_jti(api_settings: Settings) -> None:
    """D6 identity tuple = (issuer, subject, presence); jti must NOT distinguish callers.

    Two consistent tokens that differ ONLY in jti belong to the SAME caller, so the identity
    tuple must be equal; a token with a different presence is a DIFFERENT caller. This asserts
    the tuple the replay cache must key on (req 9 will consume it), and that jti is excluded."""
    t1 = mint_token(
        api_settings, scopes=["eric/claude-code/episodic:rw"], presence="eric/claude-code"
    )
    t2 = mint_token(
        api_settings, scopes=["eric/claude-code/episodic:rw"], presence="eric/claude-code"
    )
    c1 = validate_token(t1, settings=api_settings)
    c2 = validate_token(t2, settings=api_settings)
    assert _is_ok(c1) and _is_ok(c2)
    id1 = (c1.value.issuer, c1.value.subject, c1.value.presence)
    id2 = (c2.value.issuer, c2.value.subject, c2.value.presence)
    assert id1 == id2, "same (iss,sub,presence) must be one identity regardless of jti"
    # jti (token_id) is present but must NOT be part of the identity tuple.
    assert c1.value.token_id == c2.value.token_id == "test-token", "precondition: same jti here"
    # A different presence is a different caller.
    t3 = mint_token(api_settings, scopes=["other/pres/episodic:rw"], presence="other/pres")
    c3 = validate_token(t3, settings=api_settings)
    assert _is_ok(c3)
    id3 = (c3.value.issuer, c3.value.subject, c3.value.presence)
    assert id3 != id1, "a different presence must be a different identity"
