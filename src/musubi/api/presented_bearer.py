"""REQ-8 — reject a PRESENTED-but-invalid bearer on every route, public included.

Issue #413. Before this, a public route ignored the ``Authorization`` header
entirely: ``GET /v1/ops/health`` with ``Bearer not-a-real-token`` returned 200,
byte-identical to the anonymous response. The client believed it was
authenticated and nothing said otherwise.

Absent credentials still stay public — that is a different fact and a 200.

Why ASGI middleware rather than a route dependency: the hole is on routes that
have no auth dependency *by design*, and it also covers ``/v1/docs`` and
``/v1/openapi.json``, which are framework-generated and carry no dependency of
ours at all. A dependency cannot reach them; middleware sees every request.

Mounted INNERMOST of the response-observing middleware (added before the
idempotency observer and the metrics middleware) so a rejection is still
observed and counted like any other terminal response rather than bypassing
telemetry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from starlette.datastructures import Headers

from musubi.api.errors import ErrorCode, error_response
from musubi.auth.middleware import PRESENTED_AUTH_STATE_KEY, presented_bearer_context
from musubi.types.common import Err

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from musubi.settings import Settings


class PresentedBearerGuard:
    """Serve a typed 401 when a request PRESENTS an invalid bearer credential."""

    def __init__(self, app: ASGIApp, settings: Settings | None = None) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        result = presented_bearer_context(
            Headers(scope=scope).get("authorization"), settings=self.settings
        )
        if isinstance(result, Err):
            error = result.error
            response = error_response(
                status_code=error.status_code,
                detail=error.detail,
                code=cast(ErrorCode, error.code),
                # Truthful for BOTH route classes. The first revision said "omit it to
                # call a public route", which is false on a protected route — omitting
                # there is still a 401.
                hint=(
                    "present a valid bearer credential; omitting it succeeds only on "
                    "documented public routes"
                ),
            )
            await response(scope, receive, send)
            return

        if result.value is not None:
            # Carry the edge-validated context so authenticate_request does NOT
            # cryptographically re-validate the same token in the handler. Keyed by the
            # token itself, so the decision can only satisfy the credential it came from.
            token = str(Headers(scope=scope).get("authorization") or "").partition(" ")[2]
            scope.setdefault("state", {})[PRESENTED_AUTH_STATE_KEY] = (
                token.strip(),
                result.value,
            )

        await self.app(scope, receive, send)


__all__ = ["PresentedBearerGuard"]
