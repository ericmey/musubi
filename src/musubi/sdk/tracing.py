"""OpenTelemetry integration for the Musubi SDK.

Optional dependency: `pip install "musubi[otel]"`.
If `opentelemetry-api` is not installed, all tracing helpers become
zero-overhead no-ops.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace

    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False


def scrub_url(url: str) -> str:
    """Scrub sensitive credentials (e.g. bearer tokens, passwords) from a URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.password or parsed.username:
            parsed = parsed._replace(
                netloc=(parsed.hostname or "") + (f":{parsed.port}" if parsed.port else "")
            )
        return urllib.parse.urlunparse(parsed)
    except Exception:
        return url


@contextmanager
def sdk_span(
    operation_name: str,
    http_method: str,
    url: str,
    namespace: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """Emit an OpenTelemetry span around an SDK HTTP call.

    No-op if opentelemetry-api is not installed.
    """
    if not HAS_OTEL:
        yield
        return

    tracer = trace.get_tracer("musubi.sdk")
    span_name = f"musubi.{operation_name}"

    attributes: dict[str, Any] = {
        "http.method": http_method,
        "http.url": scrub_url(url),
    }
    if namespace:
        attributes["musubi.namespace"] = namespace
    if request_id:
        attributes["musubi.request_id"] = request_id

    start_time = time.time()

    # Use contextlib's enter/exit logic explicitly to support start_as_current_span
    with tracer.start_as_current_span(span_name, attributes=attributes) as span:
        try:
            yield
        finally:
            duration_ms = (time.time() - start_time) * 1000.0
            span.set_attribute("musubi.duration_ms", duration_ms)


__all__ = ["HAS_OTEL", "scrub_url", "sdk_span"]
