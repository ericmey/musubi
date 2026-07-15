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


def enforce_namespace_policy(
    context: AuthContext,
    *,
    targets: list[tuple[str, str]],
    access: AccessLevel = "r",
) -> Result[list[tuple[str, str]], ScopeError]:
    """AUTH-001: the shared READ-ONLY enforcement seam.

    Drops any target whose namespace is in ``context.excluded_namespaces``
    (the canonical per-agent exclusion list composed at token-validation
    time: mandatory baseline UNION per-agent settings UNION token
    additions), then runs ``resolve_namespace_scope(... access=access)``
    on each surviving target. Returns the validated targets (or the
    first ``ScopeError``).

    This is the single source of truth for the exclusion policy. Every
    read entry point (HTTP ``/v1/retrieve``, ``/v1/context``,
    ``/v1/retrieve/stream``, SDK, adapter, voice, auth middleware)
    calls this function exactly once, after target resolution and
    wildcard expansion. Hardcoding route-specific exclusions is a
    code-review must-fix.

    The seam is READ-ONLY. The write path runs the existing
    ``resolve_namespace_scope(... access=\"w\")`` flow unchanged;
    ``excluded_namespaces`` is NOT applied to writes.

    The seam is intrinsic: the per-agent exclusion list is the only
    input. No corpus scan, no hand-picked weight, no per-plane
    calibration table. The composition is additive (the token
    cannot subtract from the mandatory baseline).
    """
    if access != "r":
        # READ-ONLY contract: the seam is not invoked on the write
        # path. A write to an excluded namespace is permitted under
        # the existing write scope. See the write flow in the
        # per-route handlers (the per-target ``resolve_namespace_scope``
        # call is unchanged for ``access=\"w\"``).
        pass

    excluded = context.excluded_namespaces
    filtered: list[tuple[str, str]] = []
    for ns, plane in targets:
        if ns in excluded:
            continue
        filtered.append((ns, plane))
    if not filtered:
        return Ok(value=[])

    # Run the per-namespace scope check on each surviving target.
    # The first failure short-circuits with the ``ScopeError`` so the
    # caller gets a clean 403 rather than a partial response.
    validated: list[tuple[str, str]] = []
    for ns, plane in filtered:
        result = resolve_namespace_scope(context, namespace=ns, access=access)
        if isinstance(result, Err):
            return Err(error=result.error)
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
