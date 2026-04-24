"""FastAPI app factory + middleware chain.

``create_app(settings=None)`` is the production + test entry point.
Middleware order matters:

1. **Correlation ID** — read ``X-Request-Id`` if present, mint one
   otherwise. Echo on the response. Available to log lines via
   ``request.state.correlation_id``.
2. **Idempotency cache** — for write endpoints carrying an
   ``Idempotency-Key`` header, hit the cache before invoking the
   handler. Cache hits return the original response with
   ``X-Idempotent-Replay: true``; same key + different body is a
   ``CONFLICT`` 409.
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
from musubi.api.idempotency import IdempotencyCache, get_idempotency_cache
from musubi.api.rate_limit import DEFAULT_BUCKETS, RateLimiter, get_rate_limiter
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
    writes_artifact,
    writes_concept,
    writes_curated,
    writes_episodic,
    writes_lifecycle,
    writes_retrieve_stream,
    writes_thoughts,
)
from musubi.observability import install_metrics_middleware, request_id_var
from musubi.settings import Settings

log = logging.getLogger(__name__)

_CORRELATION_HEADER = "X-Request-Id"
_IDEMPOTENCY_HEADER = "Idempotency-Key"
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
        """Apply rate-limit + idempotency middleware around ``call_next``.

        Both apply only to write methods. Read endpoints pass straight
        through (the rate-limit + idempotency ceilings are write-side
        per the canonical-api spec)."""
        if request.method not in _WRITE_METHODS:
            return await call_next(request)

        cache: IdempotencyCache = get_idempotency_cache()
        limiter: RateLimiter = get_rate_limiter()
        bearer = _bearer_from(request)
        operator = _is_operator(request)

        # ------ Idempotency ------
        idem_key = request.headers.get(_IDEMPOTENCY_HEADER)
        body_bytes: bytes | None = None
        if idem_key:
            body_bytes = await request.body()
            try:
                body_for_hash = json.loads(body_bytes) if body_bytes else None
            except json.JSONDecodeError:
                body_for_hash = body_bytes.decode("utf-8", errors="replace")
            status, cached_body, cached_status = cache.lookup(idem_key, body_for_hash)
            if status == "hit" and cached_body is not None and cached_status is not None:
                resp = Response(
                    content=json.dumps(cached_body).encode("utf-8"),
                    status_code=cached_status,
                    media_type="application/json",
                )
                resp.headers["X-Idempotent-Replay"] = "true"
                return resp
            if status == "conflict":
                return error_response(
                    status_code=409,
                    detail="Idempotency-Key reused with a different body",
                    code="CONFLICT",
                    hint="use a fresh key, or send the original body",
                )
            # Re-attach the consumed body so downstream parsing works.
            if body_bytes is not None:
                _captured_body = body_bytes

                async def _replay() -> dict[str, object]:
                    return {"type": "http.request", "body": _captured_body, "more_body": False}

                request._receive = _replay

        # ------ Rate limit ------
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

        # ------ Idempotency cache store (after success) ------
        if idem_key and 200 <= response.status_code < 300:
            try:
                resp_bytes = b""
                async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                    resp_bytes += chunk
                resp_body = json.loads(resp_bytes) if resp_bytes else {}
                cache.store(
                    idem_key,
                    body_for_hash if idem_key else None,
                    response_status=response.status_code,
                    response_body=resp_body if isinstance(resp_body, dict) else {},
                )
                rebuilt: Response = Response(
                    content=resp_bytes,
                    status_code=response.status_code,
                    media_type=response.media_type,
                    headers=dict(response.headers),
                )
                response = rebuilt
            except (AttributeError, json.JSONDecodeError):
                # Streaming responses or non-JSON; skip caching but
                # don't fail the request.
                pass
        return response

    app.add_exception_handler(APIError, api_error_handler)

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
