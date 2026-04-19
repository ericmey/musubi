"""FastAPI-compatible bearer-token authentication helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from musubi.auth.scopes import (
    AccessLevel,
    ScopeError,
    ScopeGrant,
    require_operator_scope,
    resolve_namespace_scope,
)
from musubi.auth.tokens import AuthContext, TokenValidationError, validate_token
from musubi.settings import Settings
from musubi.types.common import Err, Ok, Result


class _HeadersLike(Protocol):
    def get(self, key: str, default: object | None = None) -> object | None: ...


class _StateLike(Protocol):
    auth: AuthContext


class _RequestLike(Protocol):
    headers: _HeadersLike
    state: _StateLike


@dataclass(frozen=True)
class AuthRequirement:
    """Authorization needed for a route."""

    namespace: str | None = None
    access: AccessLevel = "r"
    operator: bool = False


@dataclass(frozen=True)
class AuthHTTPError:
    """HTTP-shaped auth error for FastAPI dependencies/middleware."""

    status_code: int
    code: str
    detail: str


def authenticate_request(
    request: _RequestLike,
    requirement: AuthRequirement | None = None,
    *,
    settings: Settings | None = None,
) -> Result[AuthContext, AuthHTTPError]:
    """Validate bearer auth, check optional scope, and attach context to request state."""

    bearer = _bearer_token(request.headers)
    if bearer is None:
        return Err(
            error=AuthHTTPError(
                status_code=401,
                code="UNAUTHORIZED",
                detail="missing bearer token",
            )
        )

    token_result = validate_token(bearer, settings=settings)
    if isinstance(token_result, Err):
        return Err(error=_http_error_from_token_error(token_result.error))

    context = token_result.value
    if requirement is not None:
        scope_result = _check_requirement(context, requirement)
        if isinstance(scope_result, Err):
            return Err(error=_http_error_from_scope_error(scope_result.error))

    request.state.auth = context
    return Ok(value=context)


def _check_requirement(
    context: AuthContext,
    requirement: AuthRequirement,
) -> Result[AuthContext | ScopeGrant, ScopeError]:
    if requirement.operator:
        operator_result = require_operator_scope(context)
        if isinstance(operator_result, Err):
            return Err(error=operator_result.error)
        return Ok(value=operator_result.value)
    if requirement.namespace is not None:
        namespace_result = resolve_namespace_scope(
            context,
            namespace=requirement.namespace,
            access=requirement.access,
        )
        if isinstance(namespace_result, Err):
            return Err(error=namespace_result.error)
        return Ok(value=namespace_result.value)
    return Ok(value=context)


def _bearer_token(headers: _HeadersLike) -> str | None:
    authorization = headers.get("authorization")
    if authorization is None:
        authorization = headers.get("Authorization")
    if not isinstance(authorization, str):
        return None

    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


def _http_error_from_token_error(error: TokenValidationError) -> AuthHTTPError:
    return AuthHTTPError(
        status_code=error.status_code,
        code=error.code,
        detail=error.detail,
    )


def _http_error_from_scope_error(error: ScopeError) -> AuthHTTPError:
    return AuthHTTPError(
        status_code=error.status_code,
        code=error.code,
        detail=error.detail,
    )


__all__ = ["AuthHTTPError", "AuthRequirement", "authenticate_request"]
