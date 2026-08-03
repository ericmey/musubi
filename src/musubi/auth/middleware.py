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

    cached = _reuse_presented_context(request, bearer)
    if cached is not None:
        context = cached
    else:
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


#: Private request-state seam. The REQ-8 guard cryptographically validates a presented
#: bearer at the edge; :func:`authenticate_request` then reuses that result instead of
#: validating the SAME token a second time in the handler.
#:
#: Without this, every protected request validated twice — reproduced by Yua on the
#: first revision of #413: a valid GET /v1/namespaces returned 200 with
#: ``validate_token`` call_count=2. ``tokens.py`` has no JWKS cache, so under RS256 that
#: is two synchronous JWKS fetches per protected request, and it amplifies exactly the
#: Kong path this issue deliberately bounded.
#:
#: Stored as (token, context) and matched on the token, so the cached decision can only
#: satisfy the credential it was actually derived from.
PRESENTED_AUTH_STATE_KEY = "_musubi_presented_auth"


def presented_bearer_context(
    authorization: str | None,
    *,
    settings: Settings | None = None,
) -> Result[AuthContext | None, AuthHTTPError]:
    """REQ-8 edge decision, carrying the validated context so nobody validates twice.

    ``Ok(None)``    — no bearer was presented; proceed (public routes stay public).
    ``Ok(context)`` — a valid bearer was presented; proceed AND reuse this context.
    ``Err(error)``  — a bearer was presented and is not valid; serve the error.

    Takes the RAW ``Authorization`` value (``None`` when absent) rather than a headers
    object, so the decision is a pure string function and depends on no framework's
    header type.

    A client that sends ``Authorization: Bearer …`` is asserting an identity. Serving
    it anonymously tells that client it is authenticated when it is not — the failure
    is silent and lands on the caller. Absent credentials stay public; that is a
    different fact and stays a 200.

    Deliberately NOT reusing :func:`_bearer_token`, which collapses "absent", "not a
    bearer scheme" and "bearer with an empty value" all to ``None`` — correct when
    REQUIRING auth, wrong when DETECTING presentation. An empty ``Bearer`` would read
    as absent and slip through the very hole REQ-8 closes.

    Scope, deliberately narrow (Issue #413): only the ``Bearer`` scheme is an
    assertion. A non-bearer ``Authorization`` (``Basic …``) passes through, because
    rejecting it is policy REQ-8 does not state and no repo caller exercises. Widen it
    in a slice that says so, not here.
    """
    if not isinstance(authorization, str) or not authorization.strip():
        return Ok(value=None)  # absent — public routes stay public (REQ-8 control 1)

    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return Ok(value=None)  # not a presented bearer — out of scope, see above

    token = value.strip()
    if not token:
        return Err(
            error=AuthHTTPError(
                status_code=401,
                code="UNAUTHORIZED",
                detail="presented bearer credential is empty",
            )
        )

    result = validate_token(token, settings=settings)
    if isinstance(result, Err):
        return Err(error=_http_error_from_token_error(result.error))
    return Ok(value=result.value)


def _reuse_presented_context(request: _RequestLike, bearer: str) -> AuthContext | None:
    """Return the edge-validated context for ``bearer``, or None to validate here.

    Scope/operator requirements are NOT cached — only the cryptographic validation is.
    The caller still runs :func:`_check_requirement` against the reused context, so a
    protected route keeps enforcing its AuthRequirement exactly as before.
    """
    state = getattr(request, "state", None)
    entry = getattr(state, PRESENTED_AUTH_STATE_KEY, None) if state is not None else None
    if not isinstance(entry, tuple) or len(entry) != 2:
        return None
    cached_token, cached_context = entry
    if cached_token != bearer or not isinstance(cached_context, AuthContext):
        return None
    return cached_context


__all__ = [
    "PRESENTED_AUTH_STATE_KEY",
    "AuthHTTPError",
    "AuthRequirement",
    "authenticate_request",
    "presented_bearer_context",
]
