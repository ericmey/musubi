"""Minimal HTTP `/metrics` exposition for processes without a FastAPI app.

Used by the lifecycle-worker (and any future standalone process that
shares :func:`musubi.observability.registry.default_registry`). The
worker is a pure asyncio tick loop — no API surface — so the in-process
Registry has no exposition path without something like this.

Pattern mirrors the API's `/v1/ops/metrics` endpoint
(``musubi.observability.metrics_middleware``) — same renderer
(:func:`render_text_format`), same Registry singleton — but exposed via
stdlib ``http.server`` in a daemon thread instead of through FastAPI.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from musubi.observability.registry import default_registry, render_text_format

logger = logging.getLogger(__name__)


class _MetricsHandler(BaseHTTPRequestHandler):
    """Serve `GET /metrics` from the process-wide registry; 404 elsewhere."""

    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render_text_format(default_registry()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default access logging: Prometheus scrapes at 15s
        # intervals; one log line per scrape is steady-state noise. Real
        # errors still surface via ``log_error`` / exception logging.
        return


def start_metrics_server(
    port: int,
    *,
    host: str = "0.0.0.0",
) -> threading.Thread:
    """Start the metrics HTTP server in a daemon thread; return the thread.

    Daemon so process shutdown is never blocked on the server. ``host``
    defaults to all interfaces — production deploys do not host-bind the
    port (Prometheus reaches it via the compose internal network).

    The thread is started before return; the caller does not need to
    ``.start()`` it. Pass ``port=0`` to let the OS pick a port (tests
    use this; production passes a fixed port from settings).
    """
    httpd = HTTPServer((host, port), _MetricsHandler)

    def _serve() -> None:
        bound_port = httpd.server_address[1]
        logger.info("metrics-server listening on %s:%d/metrics", host, bound_port)
        try:
            httpd.serve_forever()
        except Exception:
            logger.exception("metrics-server crashed")

    thread = threading.Thread(target=_serve, name="metrics-server", daemon=True)
    thread.start()
    # Attach the server so callers (tests) can read the bound port + shut
    # it down deterministically. Not part of the public API contract;
    # production callers ignore both.
    thread.httpd = httpd  # type: ignore[attr-defined]
    return thread


__all__ = ["start_metrics_server"]
