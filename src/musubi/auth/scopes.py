"""Namespace and special-scope authorization checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from musubi.auth.tokens import AuthContext
from musubi.types.common import Err, Ok, Result

logger = logging.getLogger(__name__)

AccessLevel = Literal["r", "w"]


@dataclass(frozen=True)
class ScopeError:
    """Token is valid but does not grant the requested access."""

    detail: str
    code: str = "FORBIDDEN"
    status_code: int = 403


class ScopeGrant(BaseModel):
    """A successful scope decision."""

    model_config = ConfigDict(frozen=True)

    subject: str
    namespace: str
    access: AccessLevel
    scope_used: str


def resolve_namespace_scope(
    context: AuthContext,
    *,
    namespace: str,
    access: AccessLevel,
) -> Result[ScopeGrant, ScopeError]:
    """Resolve a token's namespace scopes against a requested namespace/access."""

    for scope in context.scopes:
        if _namespace_scope_allows(scope, namespace, access):
            _audit("auth.allow", context, namespace=namespace, scope_used=scope, access=access)
            return Ok(
                value=ScopeGrant(
                    subject=context.subject,
                    namespace=namespace,
                    access=access,
                    scope_used=scope,
                )
            )

    detail = f"namespace {namespace!r} not in token scope for {access!r} access"
    _audit("auth.deny", context, namespace=namespace, scope_used=None, access=access, reason=detail)
    return Err(error=ScopeError(detail=detail))


def resolve_blended_query_scope(
    context: AuthContext,
    *,
    namespace: str,
    underlying_namespaces: tuple[str, ...],
) -> Result[ScopeGrant, ScopeError]:
    """Check every plane namespace a blended query expands to."""

    missing: list[str] = []
    grants: list[str] = []
    for plane_namespace in underlying_namespaces:
        result = resolve_namespace_scope(context, namespace=plane_namespace, access="r")
        if isinstance(result, Err):
            missing.append(plane_namespace)
        else:
            grants.append(result.value.scope_used)

    if missing:
        detail = f"blended namespace {namespace!r} requires read scope for {', '.join(missing)}"
        _audit(
            "auth.deny", context, namespace=namespace, scope_used=None, access="r", reason=detail
        )
        return Err(error=ScopeError(detail=detail))

    scope_used = ",".join(grants)
    _audit("auth.allow", context, namespace=namespace, scope_used=scope_used, access="r")
    return Ok(
        value=ScopeGrant(
            subject=context.subject,
            namespace=namespace,
            access="r",
            scope_used=scope_used,
        )
    )


def require_operator_scope(context: AuthContext) -> Result[ScopeGrant, ScopeError]:
    """Require the special ``operator`` scope for admin endpoints."""

    if "operator" in context.scopes:
        _audit("auth.allow", context, namespace="operator", scope_used="operator", access="w")
        return Ok(
            value=ScopeGrant(
                subject=context.subject,
                namespace="operator",
                access="w",
                scope_used="operator",
            )
        )

    detail = "operator scope required"
    _audit("auth.deny", context, namespace="operator", scope_used=None, access="w", reason=detail)
    return Err(error=ScopeError(detail=detail))


def require_thought_check_scope(
    context: AuthContext,
    *,
    presence: str,
) -> Result[ScopeGrant, ScopeError]:
    """Require ``thoughts:check:<presence>`` for the presence's inbox."""

    requested_presence = presence.rsplit("/", maxsplit=1)[-1]
    token_presence = context.presence.rsplit("/", maxsplit=1)[-1]
    expected_scope = f"thoughts:check:{requested_presence}"

    if requested_presence == token_presence and expected_scope in context.scopes:
        _audit(
            "auth.allow", context, namespace=expected_scope, scope_used=expected_scope, access="r"
        )
        return Ok(
            value=ScopeGrant(
                subject=context.subject,
                namespace=expected_scope,
                access="r",
                scope_used=expected_scope,
            )
        )

    detail = f"thought check scope for {presence!r} not in token scope"
    _audit(
        "auth.deny", context, namespace=expected_scope, scope_used=None, access="r", reason=detail
    )
    return Err(error=ScopeError(detail=detail))


def require_thought_send_scope(context: AuthContext) -> Result[ScopeGrant, ScopeError]:
    """Require ``thoughts:send`` for thought send endpoints."""

    if "thoughts:send" in context.scopes:
        _audit(
            "auth.allow", context, namespace="thoughts:send", scope_used="thoughts:send", access="w"
        )
        return Ok(
            value=ScopeGrant(
                subject=context.subject,
                namespace="thoughts:send",
                access="w",
                scope_used="thoughts:send",
            )
        )

    detail = "thoughts:send scope required"
    _audit(
        "auth.deny", context, namespace="thoughts:send", scope_used=None, access="w", reason=detail
    )
    return Err(error=ScopeError(detail=detail))


def _namespace_scope_allows(scope: str, namespace: str, access: AccessLevel) -> bool:
    parsed = _parse_namespace_scope(scope)
    if parsed is None:
        return False

    pattern, granted_access = parsed
    return _namespace_matches(pattern, namespace) and _access_allows(granted_access, access)


def _parse_namespace_scope(scope: str) -> tuple[str, str] | None:
    if ":" not in scope:
        return None
    namespace_glob, access = scope.rsplit(":", maxsplit=1)
    if access not in {"r", "w", "rw"}:
        return None
    return namespace_glob, access


def _namespace_matches(pattern: str, namespace: str) -> bool:
    if pattern == "**":
        return True

    pattern_parts = pattern.split("/")
    namespace_parts = namespace.split("/")
    if len(pattern_parts) != len(namespace_parts):
        return False

    return all(
        pattern_part == "*" or pattern_part == namespace_part
        for pattern_part, namespace_part in zip(pattern_parts, namespace_parts, strict=True)
    )


def _access_allows(granted: str, requested: AccessLevel) -> bool:
    return granted == "rw" or granted == requested


def _audit(
    event: str,
    context: AuthContext,
    *,
    namespace: str,
    scope_used: str | None,
    access: AccessLevel,
    reason: str | None = None,
) -> None:
    extra = {
        "event": event,
        "sub": context.subject,
        "namespace": namespace,
        "access": access,
        "scope_used": scope_used,
    }
    if reason is not None:
        extra["reason"] = reason
    logger.info(event, extra=extra)


__all__ = [
    "AccessLevel",
    "ScopeError",
    "ScopeGrant",
    "require_operator_scope",
    "require_thought_check_scope",
    "require_thought_send_scope",
    "resolve_blended_query_scope",
    "resolve_namespace_scope",
]
