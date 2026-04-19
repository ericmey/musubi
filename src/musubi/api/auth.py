"""FastAPI auth dependency wrapping :func:`musubi.auth.authenticate_request`.

A route protected by ``require_auth()`` returns 401 / 403 + the typed
error envelope when the bearer token is missing, invalid, or out of
scope. The :class:`AuthContext` is attached to ``request.state.auth``
for downstream router code to read.

The cast on ``request`` below silences a Protocol-vs-FastAPI mypy
mismatch: ``musubi.auth.middleware._RequestLike`` declares ``headers``
+ ``state`` as settable attributes, while ``fastapi.Request`` exposes
them as read-only properties (``state`` itself is mutable, the
attribute reference isn't). Runtime behaviour is identical; the
ignore is scoped to the single dispatch point.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import Depends, Request

from musubi.api.dependencies import get_settings_dep
from musubi.api.errors import APIError, ErrorCode
from musubi.auth import AuthRequirement, authenticate_request
from musubi.settings import Settings
from musubi.types.common import Err


def require_auth(
    namespace_qs_param: str = "namespace",
    *,
    operator: bool = False,
    access: Literal["r", "w"] = "r",
) -> Callable[[Request, Settings], None]:
    """Build a FastAPI dependency that authenticates + checks scope.

    The dependency reads the ``namespace`` from a query parameter (or
    skips the namespace check when called for non-namespace-scoped
    routes like ``/v1/namespaces``). Operator-only routes pass
    ``operator=True``.
    """

    def _dep(
        request: Request,
        settings: Settings = Depends(get_settings_dep),
    ) -> None:
        ns = request.query_params.get(namespace_qs_param) if not operator else None
        requirement = AuthRequirement(
            namespace=ns,
            access=access,
            operator=operator,
        )
        result = authenticate_request(
            request,  # type: ignore[arg-type]
            requirement,
            settings=settings,
        )
        if isinstance(result, Err):
            err = result.error
            code: ErrorCode = err.code  # type: ignore[assignment]
            raise APIError(
                status_code=err.status_code,
                code=code,
                detail=err.detail,
            )

    return _dep


def require_operator() -> Callable[[Request, Settings], None]:
    """Sugar — operator-scoped dependency."""
    return require_auth(operator=True)


__all__ = ["require_auth", "require_operator"]
