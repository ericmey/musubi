"""FastAPI metrics middleware — per-endpoint counter + latency + 5xx.

Per [[09-operations/observability]] § Core metrics (the
``musubi_http_*`` family). Installed once on the FastAPI app via
:func:`install_metrics_middleware`; observes every inbound request and
emits to the supplied :class:`Registry`.

The metrics surface itself (``/v1/ops/metrics``) is served by the API's
ops router; this middleware ONLY produces the data, never serves it.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from musubi.observability.registry import Registry, default_registry

_INSTALLED_FLAG = "_musubi_metrics_installed"
_SELF_SKIP_PATHS: frozenset[str] = frozenset({"/v1/ops/metrics", "/metrics"})


def install_metrics_middleware(
    app: FastAPI,
    *,
    registry: Registry | None = None,
) -> None:
    """Install per-endpoint metrics on ``app``. Idempotent — calling
    twice on the same app is a no-op."""
    if getattr(app.state, _INSTALLED_FLAG, False):
        return
    reg = registry or default_registry()

    requests_total = reg.counter(
        "musubi_http_requests_total",
        "HTTP requests by endpoint, method, status",
        labelnames=("endpoint", "method", "status"),
    )
    duration_ms = reg.histogram(
        "musubi_http_request_duration_ms",
        "HTTP request duration in milliseconds",
        labelnames=("endpoint", "method"),
        buckets=(5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0),
    )
    five_xx_total = reg.counter(
        "musubi_5xx_total",
        "5xx responses by endpoint",
        labelnames=("endpoint",),
    )

    @app.middleware("http")
    async def metrics_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _SELF_SKIP_PATHS:
            return await call_next(request)
        method = request.method
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            duration_ms.labels(endpoint=path, method=method).observe(elapsed_ms)
            requests_total.labels(endpoint=path, method=method, status="500").inc()
            five_xx_total.labels(endpoint=path).inc()
            raise
        elapsed_ms = (time.monotonic() - start) * 1000.0
        duration_ms.labels(endpoint=path, method=method).observe(elapsed_ms)
        requests_total.labels(endpoint=path, method=method, status=str(response.status_code)).inc()
        if response.status_code >= 500:
            five_xx_total.labels(endpoint=path).inc()
        return response

    app.state._musubi_metrics_installed = True


__all__ = ["install_metrics_middleware"]
