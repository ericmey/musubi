"""JWT validation for Musubi bearer tokens."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

import httpx
import jwt
from jwt import PyJWK
from jwt.algorithms import AllowedPublicKeys
from pydantic import BaseModel, ConfigDict

from musubi.config import get_settings
from musubi.settings import Settings
from musubi.types.common import Err, Ok, Result

_AUDIENCE = "musubi"
_SUPPORTED_ALGORITHMS = ("HS256", "RS256")


@dataclass(frozen=True)
class InvalidTokenError:
    """Token is malformed, has an invalid signature, or fails claim validation."""

    detail: str
    code: str = "UNAUTHORIZED"
    status_code: int = 401


@dataclass(frozen=True)
class ExpiredTokenError:
    """Token signature is valid but the token is expired."""

    detail: str = "token expired"
    code: str = "UNAUTHORIZED"
    status_code: int = 401


class AuthContext(BaseModel):
    """Validated auth claims carried by route handlers and plane calls."""

    model_config = ConfigDict(frozen=True)

    subject: str
    issuer: str
    audience: str
    scopes: tuple[str, ...]
    presence: str
    token_id: str | None = None


type TokenValidationError = InvalidTokenError | ExpiredTokenError


def validate_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> Result[AuthContext, TokenValidationError]:
    """Validate a JWT bearer token against configured issuer, audience, and keys."""

    active_settings = settings or get_settings()
    header_result = _token_header(token)
    if isinstance(header_result, Err):
        return Err(error=header_result.error)

    alg = header_result.value.get("alg")
    if alg not in _SUPPORTED_ALGORITHMS:
        return Err(error=InvalidTokenError(detail="unsupported token signing algorithm"))

    key_result = _verification_key(token, header_result.value, active_settings)
    if isinstance(key_result, Err):
        return Err(error=key_result.error)

    try:
        payload = jwt.decode(
            token,
            key_result.value,
            algorithms=[cast(str, alg)],
            audience=_AUDIENCE,
            issuer=_issuer(active_settings),
        )
    except jwt.ExpiredSignatureError:
        return Err(error=ExpiredTokenError())
    except jwt.PyJWTError as exc:
        return Err(error=InvalidTokenError(detail=str(exc)))

    context_result = _context_from_payload(payload)
    if isinstance(context_result, Err):
        return Err(error=context_result.error)
    return context_result


def _token_header(token: str) -> Result[dict[str, Any], InvalidTokenError]:
    try:
        return Ok(value=jwt.get_unverified_header(token))
    except jwt.PyJWTError as exc:
        return Err(error=InvalidTokenError(detail=str(exc)))


def _verification_key(
    token: str,
    header: dict[str, Any],
    settings: Settings,
) -> Result[str | AllowedPublicKeys | PyJWK, InvalidTokenError]:
    algorithm = header.get("alg")
    if algorithm == "HS256":
        return Ok(value=settings.jwt_signing_key.get_secret_value())
    if algorithm == "RS256":
        rs256_result = _rs256_key(token, header, settings)
        if isinstance(rs256_result, Err):
            return Err(error=rs256_result.error)
        return Ok(value=rs256_result.value)
    return Err(error=InvalidTokenError(detail="unsupported token signing algorithm"))


def _rs256_key(
    _token: str,
    header: dict[str, Any],
    settings: Settings,
) -> Result[AllowedPublicKeys, InvalidTokenError]:
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return Err(error=InvalidTokenError(detail="RS256 token missing kid header"))

    jwks_result = _fetch_jwks(settings)
    if isinstance(jwks_result, Err):
        return Err(error=jwks_result.error)

    for jwk in jwks_result.value.get("keys", []):
        if isinstance(jwk, dict) and jwk.get("kid") == kid:
            try:
                key = cast(AllowedPublicKeys, jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk)))
                return Ok(value=key)
            except (jwt.PyJWTError, TypeError, ValueError) as exc:
                return Err(error=InvalidTokenError(detail=f"invalid jwk: {exc}"))
    return Err(error=InvalidTokenError(detail="no matching jwk for token kid"))


def _fetch_jwks(settings: Settings) -> Result[dict[str, Any], InvalidTokenError]:
    try:
        response = httpx.get(_jwks_url(settings), timeout=5.0)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return Err(error=InvalidTokenError(detail=f"jwks fetch failed: {exc}"))

    if not isinstance(body, dict):
        return Err(error=InvalidTokenError(detail="jwks response must be an object"))
    return Ok(value=cast(dict[str, Any], body))


def _context_from_payload(payload: dict[str, Any]) -> Result[AuthContext, InvalidTokenError]:
    subject = payload.get("sub")
    issuer = payload.get("iss")
    audience = payload.get("aud")
    scopes = payload.get("scope")
    presence = payload.get("presence")
    token_id = payload.get("jti")

    if not isinstance(subject, str) or not subject:
        return Err(error=InvalidTokenError(detail="token missing sub claim"))
    if not isinstance(issuer, str) or not issuer:
        return Err(error=InvalidTokenError(detail="token missing iss claim"))
    if not isinstance(audience, str) or not audience:
        return Err(error=InvalidTokenError(detail="token missing aud claim"))
    if not isinstance(presence, str) or not presence:
        return Err(error=InvalidTokenError(detail="token missing presence claim"))
    if token_id is not None and not isinstance(token_id, str):
        return Err(error=InvalidTokenError(detail="token jti claim must be a string"))

    parsed_scopes = _parse_scopes(scopes)
    if parsed_scopes is None:
        return Err(error=InvalidTokenError(detail="token scope claim must be a string list"))

    return Ok(
        value=AuthContext(
            subject=subject,
            issuer=issuer,
            audience=audience,
            scopes=parsed_scopes,
            presence=presence,
            token_id=token_id,
        )
    )


def _parse_scopes(scopes: object) -> tuple[str, ...] | None:
    if isinstance(scopes, str):
        return tuple(part for part in scopes.split() if part)
    if isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
        return tuple(scopes)
    return None


def _issuer(settings: Settings) -> str:
    return str(settings.oauth_authority).rstrip("/")


def _jwks_url(settings: Settings) -> str:
    return f"{_issuer(settings)}/.well-known/jwks.json"


__all__ = [
    "AuthContext",
    "ExpiredTokenError",
    "InvalidTokenError",
    "TokenValidationError",
    "validate_token",
]
