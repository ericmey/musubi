"""FastAPI app factory + middleware chain.

``create_app(settings=None)`` is the production + test entry point.
Middleware order matters:

1. **Idempotency observer** (outermost) — a store-only ASGI observer for the routed post-authz
   idempotency pipeline. It does NOT decide replay: the routed dependency
   (:mod:`musubi.api.idempotency_dependency`) runs AFTER authentication + namespace authz and
   raises :class:`Replay` on a hit (served byte-exact with ``X-Idempotent-Replay: true``), 409s a
   conflict/in-flight, and on an acquired miss publishes a lease the observer completes + releases.
   This replaced the pre-auth idempotency cache that caused SEC-002/IDEM-001.
2. **Correlation ID** — read ``X-Request-Id`` if present, mint one
   otherwise. Echo on the response. Available to log lines via
   ``request.state.correlation_id``.
3. **Rate limit** — per-token bucketed counter on write endpoints.
   Reads the bucket name from each route's ``operation_id`` (a
   ``"<name>.bucket=<bucket>"`` suffix). Operator-scoped tokens get a
   10x ceiling.
4. **Exception → typed-error envelope** — :class:`APIError` and any
   uncaught :class:`Exception` map to the spec's
   ``{"error": {"code", "detail", "hint"}}`` shape.

Per ADR-0013 the OpenAPI 3.1 document is generated at runtime from the
pydantic models; the committed ``openapi.yaml`` snapshot at the repo
root is regenerated on each API version bump and verified in tests.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError

from musubi.api.errors import APIError, api_error_handler, error_response
from musubi.api.idempotency_dependency import Replay
from musubi.api.idempotency_observer import IdempotencyObserver
from musubi.api.rate_limit import DEFAULT_BUCKETS, RateLimiter, get_rate_limiter
from musubi.api.routers import (
    artifacts,
    concepts,
    context,
    contradictions,
    curated,
    episodic,
    lifecycle,
    namespaces,
    ops,
    retrieve,
    thoughts,
    writes_artifact,
    writes_concept,
    writes_curated,
    writes_episodic,
    writes_lifecycle,
    writes_retrieve_stream,
    writes_thoughts,
)
from musubi.observability import (
    configure_logging,
    init_tracing,
    install_metrics_middleware,
    instrument_fastapi,
    request_id_var,
)
from musubi.settings import Settings

log = logging.getLogger(__name__)

_CORRELATION_HEADER = "X-Request-Id"
_WRITE_METHODS = frozenset({"POST", "PATCH", "DELETE", "PUT"})


_PATH_TO_BUCKET: tuple[tuple[str, str], ...] = (
    # Order matters — first match wins.
    ("/v1/episodic/batch", "batch-write"),
    ("/v1/episodic", "capture"),
    ("/v1/curated", "capture"),
    ("/v1/artifacts", "artifact-upload"),
    ("/v1/thoughts/send", "thought"),
    ("/v1/thoughts/read", "default"),
    ("/v1/lifecycle/transition", "transition"),
    ("/v1/concepts", "default"),
    ("/v1/context", "retrieve"),
    ("/v1/retrieve/stream", "retrieve"),
    ("/v1/retrieve", "retrieve"),
)


def _bucket_for_path(path: str, method: str) -> str:
    """Map ``(method, path)`` to a rate-limit bucket name.

    Routing hasn't happened yet at middleware time, so we match the
    URL path prefix directly instead of reading the route's
    ``operation_id``. Returns ``"default"`` if no prefix matches.
    """
    del method  # bucket is path-driven; method is informational
    for prefix, bucket in _PATH_TO_BUCKET:
        if path == prefix or path.startswith(prefix + "/"):
            return bucket
    return "default"


def _bearer_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return None


def _is_operator(request: Request) -> bool:
    """Best-effort operator check from the bearer's payload.

    Reads the JWT body's ``scope`` claim WITHOUT verifying — this is
    only used to pick the right rate-limit ceiling. The auth middleware
    that runs later does the real verification + scope check.
    """
    bearer = _bearer_from(request)
    if not bearer:
        return False
    parts = bearer.split(".")
    if len(parts) != 3:
        return False
    import base64

    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, json.JSONDecodeError):
        return False
    scopes = payload.get("scope", "")
    if isinstance(scopes, str):
        scope_list = scopes.split()
    elif isinstance(scopes, list):
        scope_list = scopes
    else:
        return False
    return "operator" in scope_list


def create_app(*, settings: Settings | None = None) -> FastAPI:
    """Build the canonical Musubi API app.

    Production: ``settings`` is loaded via :func:`musubi.config.get_settings`
    when not supplied; the production bootstrap (slice-api-app-bootstrap)
    runs at the bottom of this function to wire real Qdrant + TEI + plane
    factories into the FastAPI dep map. Tests override that via
    ``settings.musubi_skip_bootstrap=True`` (the api_factory fixture path)
    or by pre-installing dependency_overrides on the returned app.
    """
    if settings is None:
        from musubi.config import get_settings as _get_settings

        settings = _get_settings()

    # REQ-10: single-worker invariant, fail-closed. The idempotency cache is process-local, so
    # more than one worker tears it silently. `WEB_CONCURRENCY` is the standard uvicorn/gunicorn
    # launch signal — read through Settings (keeping raw environment reads out of app code) and rejected
    # here so a multi-worker boot fails loudly instead of running a torn cache. (Settings.
    # api_workers guards the config side; the systemd unit pins --workers 1.)
    if settings.web_concurrency > 1:
        raise RuntimeError(
            f"WEB_CONCURRENCY={settings.web_concurrency} > 1, but the idempotency cache is "
            f"process-local — run a single worker or move to a shared cache (fail-closed)."
        )

    # Wire structured JSON logging on root + uvicorn so the JSON-logs
    # contract from [[09-operations/observability]] § Logs is actually
    # honoured in container output. Idempotent — tests calling
    # ``create_app`` repeatedly reuse the same handler.
    configure_logging()

    # Initialize OTel tracing if Settings.otel_exporter_otlp_endpoint is
    # set. No-op otherwise; production runs unchanged when traces
    # aren't configured. Per [[09-operations/observability]] § Tracing.
    init_tracing(
        endpoint=settings.otel_exporter_otlp_endpoint or None,
        service_name=settings.otel_service_name,
        service_namespace=settings.otel_service_namespace,
        host_name=settings.otel_host_name or None,
        service_version=settings.musubi_service_version or None,
        deployment_environment=settings.otel_deployment_environment,
    )

    app = FastAPI(
        title="Musubi Core API",
        version="0.1.0",
        description=(
            "The canonical HTTP surface over Musubi Core. Read + write; full v0.1 surface."
        ),
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
    )

    # FastAPI auto-instrumentation: root span per HTTP request. Safe
    # no-op when tracing isn't initialized.
    instrument_fastapi(app)

    install_metrics_middleware(app)

    @app.middleware("http")
    async def correlation_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cid = request.headers.get(_CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = cid
        # Make the id available to structured-log records via the
        # observability contextvar; reset on response so it doesn't
        # leak into the next request handled by the same task.
        rid_token = request_id_var.set(cid)
        try:
            response = await _wrapped_call(request, call_next)
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
        request_id_var.reset(rid_token)
        return response

    async def _wrapped_call(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Apply the write-side rate limit around ``call_next``.

        Read endpoints pass straight through (the rate-limit ceilings are write-side per the
        canonical-api spec). Idempotency is NO LONGER handled here: the pre-auth cache path was
        removed in Phase B (SEC-002/IDEM-001) in favour of the routed post-authz idempotency
        dependency (:mod:`musubi.api.idempotency_dependency`) plus the store-only observer
        (:class:`~musubi.api.idempotency_observer.IdempotencyObserver`), which run AFTER
        authentication + namespace authz and bind identity to the validated principal + operation."""
        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        limiter: RateLimiter = get_rate_limiter()
        bearer = _bearer_from(request)
        operator = _is_operator(request)

        bucket_name = _bucket_for_path(request.url.path, request.method)
        bucket = DEFAULT_BUCKETS.get(bucket_name, DEFAULT_BUCKETS["default"])
        token_key = limiter.token_key(bearer)
        allowed, limit_cap, remaining, retry_after = limiter.allow(
            token_key=token_key, bucket=bucket, operator=operator
        )
        if not allowed:
            limited = error_response(
                status_code=429,
                detail=f"rate limit exceeded for bucket {bucket.name!r}",
                code="RATE_LIMITED",
                hint=f"retry after {retry_after}s",
            )
            limited.headers["X-RateLimit-Limit"] = str(limit_cap)
            limited.headers["X-RateLimit-Remaining"] = "0"
            limited.headers["Retry-After"] = str(retry_after)
            return limited

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit_cap)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    app.add_exception_handler(APIError, api_error_handler)

    @app.exception_handler(Replay)
    async def _idempotency_replay_handler(_request: Request, exc: Replay) -> Response:
        """Serve a byte-exact idempotent replay. The handler is NOT executed — the routed
        dependency raised :class:`Replay` on a cache hit. The cached ``raw_headers`` tuple is never
        mutated; the replay marker is added on a fresh header list."""
        completed = exc.completed
        response = Response(content=completed.body, status_code=completed.status)
        response.raw_headers = [*completed.raw_headers, (b"x-idempotent-replay", b"true")]
        return response

    # Mount the store-only idempotency observer OUTERMOST (Starlette wraps the most-recently-added
    # middleware last, so it sees the exact terminal response the client receives). It stores a
    # completed 2xx and releases the lease established by the routed idempotency dependency; it is a
    # no-op for every request that did not acquire a lease (reads, replays, conflicts, non-idem).
    app.add_middleware(IdempotencyObserver)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> Response:
        # 422 Unprocessable Entity is the correct status for a
        # well-formed request whose body fails semantic validation
        # (RFC 9110 §15.5.21). We were emitting 400 here originally,
        # which conflated "malformed" with "semantically invalid".
        return error_response(
            status_code=422,
            detail=str(exc),
            code="BAD_REQUEST",
            hint="check the request body / query parameters against the OpenAPI spec",
        )

    # NOTE: no global `ValidationError` handler.
    #
    # A naked `pydantic.ValidationError` can be raised anywhere — e.g.
    # when a plane rehydrates a model from Qdrant payload and the
    # stored data is corrupt. That's a 5xx, not a 422. A global
    # handler would silently map every such bug to BAD_REQUEST and
    # hide real server-side breakage. Translation is done at the
    # specific call sites where we KNOW the input is request-driven
    # (see `src/musubi/api/routers/writes_episodic.py` for the
    # capture path).

    # Read routers (from slice-api-v0-read)
    app.include_router(ops.router)
    app.include_router(episodic.router)
    app.include_router(curated.router)
    app.include_router(concepts.router)
    app.include_router(artifacts.router)
    app.include_router(thoughts.router)
    app.include_router(context.router)
    app.include_router(retrieve.router)
    app.include_router(lifecycle.router)
    app.include_router(contradictions.router)
    app.include_router(namespaces.router)

    # Write routers (slice-api-v0-write)
    app.include_router(writes_episodic.router)
    app.include_router(writes_curated.router)
    app.include_router(writes_concept.router)
    app.include_router(writes_artifact.router)
    app.include_router(writes_thoughts.router)
    app.include_router(writes_lifecycle.router)
    app.include_router(writes_retrieve_stream.router)

    # ------------------------------------------------------------------
    # Production bootstrap (slice-api-app-bootstrap, #123)
    # ------------------------------------------------------------------
    # Wires real Qdrant + TEI + every plane factory into the FastAPI
    # dep map. Skipped when settings.musubi_skip_bootstrap=True (test
    # fixtures that bypass app_factory) OR when overrides are already
    # installed. Without this, every plane endpoint 500s on first hit
    # because dependencies.py ships fail-loud NotImplementedError stubs.
    from musubi.api.bootstrap import _should_bootstrap, bootstrap_production_app

    if _should_bootstrap(app, settings):
        bootstrap_production_app(app, settings)

    return app


__all__ = ["create_app"]
