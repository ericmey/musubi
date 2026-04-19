"""FastAPI app factory + middleware chain.

``create_app(settings=None)`` is the production + test entry point.
Middleware order matters:

1. **Correlation ID** — read ``X-Request-Id`` if present, mint one
   otherwise. Echo on the response. Available to log lines via
   ``request.state.correlation_id``.
2. **Exception → typed-error envelope** — :class:`APIError` and any
   uncaught :class:`Exception` map to the spec's
   ``{"error": {"code", "detail", "hint"}}`` shape.

Per ADR-0013 the OpenAPI 3.1 document is generated at runtime from the
pydantic models; the committed ``openapi.yaml`` snapshot at the repo
root is regenerated on each API version bump and verified in tests
(``test_committed_openapi_yaml_includes_read_paths`` +
``test_runtime_openapi_matches_committed_paths``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from musubi.api.errors import APIError, api_error_handler, error_response
from musubi.api.routers import (
    artifacts,
    concepts,
    contradictions,
    curated,
    episodic,
    lifecycle,
    namespaces,
    ops,
    retrieve,
    thoughts,
)
from musubi.settings import Settings

log = logging.getLogger(__name__)

_CORRELATION_HEADER = "X-Request-Id"


def create_app(*, settings: Settings | None = None) -> FastAPI:
    """Build the canonical Musubi API app.

    ``settings`` is accepted for symmetry with other slice scaffolding
    even though FastAPI's dep injection resolves it lazily; passing it
    here is the supported test entry point.
    """
    del settings  # reserved for future eager validation; deps own runtime read
    app = FastAPI(
        title="Musubi Core API",
        version="0.1.0",
        description=(
            "The canonical HTTP surface over Musubi Core. "
            "Read-side; write surface lives in slice-api-v0-write."
        ),
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )

    # Middleware order: correlation id → exception handler.
    @app.middleware("http")
    async def correlation_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cid = request.headers.get(_CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = cid
        try:
            response = await call_next(request)
        except APIError as exc:
            response = await api_error_handler(request, exc)
        except Exception:
            log.exception(
                "api-uncaught-exception",
                extra={"correlation_id": cid, "path": request.url.path},
            )
            response = error_response(
                status_code=500,
                detail="internal error",
                code="INTERNAL",
                hint="check server logs with the X-Request-Id header value",
            )
        response.headers[_CORRELATION_HEADER] = cid
        return response

    app.add_exception_handler(APIError, api_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> Response:
        # Re-shape FastAPI's 422 into the spec's error envelope so
        # adapters see one error format across every endpoint.
        return error_response(
            status_code=400,
            detail=str(exc),
            code="BAD_REQUEST",
            hint="check the request body / query parameters against the OpenAPI spec",
        )

    # Routers — order doesn't matter; they're mounted at distinct prefixes.
    app.include_router(ops.router)
    app.include_router(episodic.router)
    app.include_router(curated.router)
    app.include_router(concepts.router)
    app.include_router(artifacts.router)
    app.include_router(thoughts.router)
    app.include_router(retrieve.router)
    app.include_router(lifecycle.router)
    app.include_router(contradictions.router)
    app.include_router(namespaces.router)

    return app


__all__ = ["create_app"]
