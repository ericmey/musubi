"""Observability — metrics, structured logs, and component health probes.

Per [[09-operations/observability]]. Three independent surfaces share
one module so the API + workers can pull `from musubi.observability
import …` without a longer dotted path:

- **Metrics:** :class:`Counter` / :class:`Histogram` / :class:`Gauge`
  via :class:`Registry`. Render the registry in Prometheus text
  format with :func:`render_text_format`. The default process-wide
  registry is :func:`default_registry`.
- **Middleware:** :func:`install_metrics_middleware` adds per-endpoint
  request counter + duration histogram + 5xx counter to a FastAPI app.
- **Logs:** :class:`StructuredJsonFormatter` produces the spec's
  JSON-per-line shape; :data:`request_id_var` is a contextvar each
  log record reads to populate ``request_id``;
  :func:`redact_token_filter` scrubs JWT-shaped strings before emit.
- **Health:** :func:`check_component_health` probes a downstream
  service's ``/health`` endpoint and returns a typed
  :class:`musubi.api.responses.ComponentStatus`.

OpenTelemetry tracing is intentionally OUT OF SCOPE per
``slice-sdk-py-otel-spans.md`` — the SDK spec scopes OTel as
opt-in; same constraint applies here.
"""

from musubi.observability.health import check_component_health
from musubi.observability.logging_setup import (
    StructuredJsonFormatter,
    redact_token_filter,
    request_id_var,
)
from musubi.observability.metrics_middleware import install_metrics_middleware
from musubi.observability.registry import (
    Counter,
    Gauge,
    Histogram,
    Registry,
    default_registry,
    render_text_format,
)

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "StructuredJsonFormatter",
    "check_component_health",
    "default_registry",
    "install_metrics_middleware",
    "redact_token_filter",
    "render_text_format",
    "request_id_var",
]
