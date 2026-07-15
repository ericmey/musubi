"""Namespace and special-scope authorization checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from musubi.auth.tokens import AuthContext
from musubi.settings import Settings
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


def enforce_namespace_policy(
    context: AuthContext,
    *,
    targets: list[tuple[str, str]],
    settings: Settings,
) -> Result[list[tuple[str, str]], ScopeError]:
    """AUTH-001: the shared READ-ONLY enforcement seam.

    FIRST runs resolve_namespace_scope on every concrete target. If any is unauthorized, it must fail (403).
    THEN filters the authorized set using Settings mandatory + subject/presence additive roots with parsed segment matching.
    Direct authorized-but-excluded exact target may return 200 empty.
    """

    # 1. Authorize all targets first
    authorized: list[tuple[str, str]] = []
    for ns, plane in targets:
        result = resolve_namespace_scope(context, namespace=ns, access="r")
        if isinstance(result, Err):
            return Err(error=result.error)
        authorized.append((ns, plane))

    if not authorized:
        return Ok(value=[])

    # 2. Filter authorized targets against exclusions
    exclusions = set(settings.default_excluded_namespaces)
    if context.subject in settings.per_agent_excluded_namespaces:
        exclusions.update(settings.per_agent_excluded_namespaces[context.subject])
    if context.presence in settings.per_agent_excluded_namespaces:
        exclusions.update(settings.per_agent_excluded_namespaces[context.presence])

    validated: list[tuple[str, str]] = []

    for ns, plane in authorized:
        parts = ns.split("/")
        excluded = False
        print(f"DEBUG: checking ns={ns} against exclusions={exclusions}")
        for ex in exclusions:
            ex_parts = ex.split("/")

            if len(ex_parts) == 1:
                if len(parts) > 1 and parts[1] == ex_parts[0]:
                    print(f"DEBUG: excluded by 1-segment {ex}")
                    excluded = True
                    break
            else:
                if parts[:len(ex_parts)] == ex_parts:
                    print(f"DEBUG: excluded by prefix {ex}")
                    excluded = True
                    break

        if not excluded:
            validated.append((ns, plane))

    return Ok(value=validated)


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


def _namespace_scope_allows(scope: str, namespace: str, access: AccessLevel) -> bool:
    parsed = _parse_namespace_scope(scope)
    if parsed is None:
        return False

    pattern, granted_access = parsed
    if pattern == "**" and access == "w":
        return False

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
    "resolve_blended_query_scope",
    "resolve_namespace_scope",
]
