"""Test Contract for ``docs/architecture/10-security/auth.md``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import AllowedPrivateKeys
from pydantic import AnyHttpUrl, SecretStr
from pytest_httpx import HTTPXMock

from musubi.auth.middleware import AuthRequirement, authenticate_request
from musubi.auth.scopes import (
    ScopeError,
    require_operator_scope,
    require_thought_check_scope,
    resolve_blended_query_scope,
    resolve_namespace_scope,
)
from musubi.auth.tokens import (
    AuthContext,
    ExpiredTokenError,
    InvalidTokenError,
    validate_token,
)
from musubi.settings import Settings
from musubi.types.common import Err, Ok


@pytest.fixture
def auth_settings() -> Settings:
    return Settings.model_validate(
        {
            "qdrant_host": "qdrant",
            "qdrant_api_key": SecretStr("test-qdrant-key"),
            "tei_dense_url": AnyHttpUrl("http://tei-dense"),
            "tei_sparse_url": AnyHttpUrl("http://tei-sparse"),
            "tei_reranker_url": AnyHttpUrl("http://tei-reranker"),
            "ollama_url": AnyHttpUrl("http://ollama:11434"),
            "embedding_model": "BAAI/bge-m3",
            "sparse_model": "naver/splade-v3",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "llm_model": "qwen2.5:7b-instruct-q4_K_M",
            "vault_path": Path("/tmp/musubi-test/vault"),
            "artifact_blob_path": Path("/tmp/musubi-test/artifacts"),
            "lifecycle_sqlite_path": Path("/tmp/musubi-test/lifecycle.sqlite"),
            "log_dir": Path("/tmp/musubi-test/log"),
            "jwt_signing_key": SecretStr("test-hs256-secret-with-at-least-32-bytes"),
            "oauth_authority": AnyHttpUrl("https://auth.example.test"),
        }
    )


@pytest.fixture
def rsa_keypair() -> Iterator[tuple[AllowedPrivateKeys, dict[str, object]]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"alg": "RS256", "kid": "kid-1", "use": "sig"})
    yield private_key, public_jwk


def _payload(
    *,
    issuer: str = "https://auth.example.test",
    subject: str = "eric-claude-code",
    scopes: list[str] | None = None,
    presence: str = "eric/claude-code",
    expires_delta: timedelta = timedelta(hours=1),
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "iss": issuer,
        "sub": subject,
        "aud": "musubi",
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": "token-123",
        "scope": scopes
        if scopes is not None
        else ["eric/claude-code/episodic:rw", "thoughts:check:claude-code"],
        "presence": presence,
    }


def _hs_token(settings: Settings, payload: dict[str, object] | None = None) -> str:
    return jwt.encode(
        payload or _payload(),
        settings.jwt_signing_key.get_secret_value(),
        algorithm="HS256",
    )


def _rs_token(
    private_key: AllowedPrivateKeys,
    payload: dict[str, object] | None = None,
    kid: str = "kid-1",
) -> str:
    return jwt.encode(payload or _payload(), private_key, algorithm="RS256", headers={"kid": kid})


def test_missing_bearer_returns_401(auth_settings: Settings) -> None:
    request = SimpleNamespace(headers={}, state=SimpleNamespace())

    result = authenticate_request(
        request,
        AuthRequirement(namespace="eric/claude-code/episodic", access="r"),
        settings=auth_settings,
    )

    assert result.is_err()
    assert isinstance(result, Err)
    assert result.error.status_code == 401
    assert result.error.code == "UNAUTHORIZED"


def test_expired_token_returns_401(auth_settings: Settings) -> None:
    token = _hs_token(auth_settings, _payload(expires_delta=timedelta(seconds=-1)))

    result = validate_token(token, settings=auth_settings)

    assert result.is_err()
    assert isinstance(result, Err)
    assert isinstance(result.error, ExpiredTokenError)


def test_wrong_issuer_returns_401(auth_settings: Settings) -> None:
    token = _hs_token(auth_settings, _payload(issuer="https://wrong-issuer.example.test"))

    result = validate_token(token, settings=auth_settings)

    assert result.is_err()
    assert isinstance(result, Err)
    assert isinstance(result.error, InvalidTokenError)
    assert result.error.status_code == 401


def test_scope_match_grants_access(
    auth_settings: Settings,
    rsa_keypair: tuple[AllowedPrivateKeys, dict[str, object]],
    httpx_mock: HTTPXMock,
) -> None:
    private_key, public_jwk = rsa_keypair
    token = _rs_token(private_key)
    httpx_mock.add_response(
        url="https://auth.example.test/.well-known/jwks.json",
        json={"keys": [public_jwk]},
    )

    token_result = validate_token(token, settings=auth_settings)
    assert token_result.is_ok()
    assert isinstance(token_result, Ok)

    scope_result = resolve_namespace_scope(
        token_result.value,
        namespace="eric/claude-code/episodic",
        access="w",
    )

    assert scope_result.is_ok()
    assert isinstance(scope_result, Ok)
    assert scope_result.value.scope_used == "eric/claude-code/episodic:rw"


def test_scope_mismatch_returns_403_with_detail(auth_settings: Settings) -> None:
    token = _hs_token(auth_settings)
    request = SimpleNamespace(
        headers={"authorization": f"Bearer {token}"},
        state=SimpleNamespace(),
    )

    result = authenticate_request(
        request,
        AuthRequirement(namespace="eric/livekit-voice/episodic", access="w"),
        settings=auth_settings,
    )

    assert result.is_err()
    assert isinstance(result, Err)
    assert result.error.status_code == 403
    assert "eric/livekit-voice/episodic" in result.error.detail


def test_operator_scope_required_for_admin_endpoints() -> None:
    without_operator = AuthContext(
        subject="eric-claude-code",
        issuer="https://auth.example.test",
        audience="musubi",
        scopes=("eric/claude-code/episodic:rw",),
        presence="eric/claude-code",
        token_id="token-123",
    )
    with_operator = AuthContext(
        subject="eric",
        issuer="https://auth.example.test",
        audience="musubi",
        scopes=("operator",),
        presence="eric/operator",
        token_id="operator-token-123",
    )

    denied = require_operator_scope(without_operator)
    allowed = require_operator_scope(with_operator)

    assert denied.is_err()
    assert isinstance(denied, Err)
    assert isinstance(denied.error, ScopeError)
    assert denied.error.status_code == 403
    assert allowed.is_ok()


def test_thought_check_scope_is_presence_specific() -> None:
    context = AuthContext(
        subject="eric-claude-code",
        issuer="https://auth.example.test",
        audience="musubi",
        scopes=("thoughts:check:claude-code",),
        presence="eric/claude-code",
        token_id="token-123",
    )

    own_inbox = require_thought_check_scope(context, presence="claude-code")
    other_inbox = require_thought_check_scope(context, presence="livekit-voice")

    assert own_inbox.is_ok()
    assert other_inbox.is_err()
    assert isinstance(other_inbox, Err)
    assert "livekit-voice" in other_inbox.error.detail


def test_blended_query_expands_and_checks_plane_scopes() -> None:
    context = AuthContext(
        subject="eric-claude-code",
        issuer="https://auth.example.test",
        audience="musubi",
        scopes=("eric/claude-code/episodic:r", "eric/_shared/curated:r"),
        presence="eric/claude-code",
        token_id="token-123",
    )

    allowed = resolve_blended_query_scope(
        context,
        namespace="eric/_shared/blended",
        underlying_namespaces=("eric/claude-code/episodic", "eric/_shared/curated"),
    )
    denied = resolve_blended_query_scope(
        context,
        namespace="eric/_shared/blended",
        underlying_namespaces=("eric/claude-code/episodic", "eric/_shared/artifact"),
    )

    assert allowed.is_ok()
    assert denied.is_err()
    assert isinstance(denied, Err)
    assert "eric/_shared/artifact" in denied.error.detail


@pytest.mark.skip(
    reason="deferred to slice-auth-authority: PKCE OAuth service is outside Core auth middleware"
)
def test_pkce_flow_end_to_end() -> None:
    raise AssertionError("covered by a future auth authority integration slice")


@pytest.mark.skip(
    reason="deferred to slice-auth-authority: refresh token storage/rotation is outside Core auth"
)
def test_refresh_token_rotation_issues_new_refresh() -> None:
    raise AssertionError("covered by a future auth authority integration slice")


@pytest.mark.skip(
    reason="deferred to slice-auth-authority: revocation cache requires the auth authority token store"
)
def test_revocation_invalidates_token_within_60s_cache() -> None:
    raise AssertionError("covered by a future auth authority integration slice")


def test_signing_key_rotation_dual_verify_period(
    auth_settings: Settings, httpx_mock: HTTPXMock
) -> None:
    old_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(old_private.public_key()))
    new_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(new_private.public_key()))
    old_jwk.update({"alg": "RS256", "kid": "old-key", "use": "sig"})
    new_jwk.update({"alg": "RS256", "kid": "new-key", "use": "sig"})
    httpx_mock.add_response(
        url="https://auth.example.test/.well-known/jwks.json",
        json={"keys": [old_jwk, new_jwk]},
    )
    httpx_mock.add_response(
        url="https://auth.example.test/.well-known/jwks.json",
        json={"keys": [old_jwk, new_jwk]},
    )

    old_token = _rs_token(old_private, kid="old-key")
    new_token = _rs_token(new_private, kid="new-key")

    assert validate_token(old_token, settings=auth_settings).is_ok()
    assert validate_token(new_token, settings=auth_settings).is_ok()


def test_every_auth_decision_emits_audit_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="musubi.auth.scopes")
    context = AuthContext(
        subject="eric-claude-code",
        issuer="https://auth.example.test",
        audience="musubi",
        scopes=("eric/claude-code/episodic:r",),
        presence="eric/claude-code",
        token_id="token-123",
    )

    resolve_namespace_scope(context, namespace="eric/claude-code/episodic", access="r")
    resolve_namespace_scope(context, namespace="eric/livekit-voice/episodic", access="r")

    messages = [record.getMessage() for record in caplog.records]
    assert "auth.allow" in messages
    assert "auth.deny" in messages


@pytest.mark.skip(
    reason="deferred to slice-auth-authority: operator token issuing belongs to CLI/service"
)
def test_operator_issued_only_via_cli() -> None:
    raise AssertionError("covered by a future auth authority CLI slice")
