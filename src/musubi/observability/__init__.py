"""Observability — metrics, structured logs, tracing, and component health probes.

Per [[09-operations/observability]]. Four independent surfaces share
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
  :func:`redact_token_filter` scrubs JWT-shaped strings before emit;
  :func:`configure_logging` re-routes uvicorn through the JSON
  formatter at app startup.
- **Tracing:** :func:`init_tracing` builds an OTel ``TracerProvider``
  exporting OTLP/gRPC to the endpoint named by
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` (e.g. ``http://shiori.mey.house:4317``);
  :func:`instrument_fastapi` installs FastAPI auto-instrumentation;
  :func:`get_tracer` returns a tracer for hand-instrumented named spans.
  All three are no-ops when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset.
- **Health:** :func:`check_component_health` probes a downstream
  service's ``/health`` endpoint and returns a typed
  :class:`musubi.api.responses.ComponentStatus`.

Tracing was scoped under ``slice-ops-observability`` per
[[09-operations/observability]] § Tracing but not shipped with the
original slice. It is being completed here (see issue #302 and the
2026-05-13 work-log entry on that slice). The earlier statement in this
docstring that OTel tracing was "OUT OF SCOPE" reflected the unshipped
state, not a design decision — it is now removed because it is no
longer accurate.
"""

from musubi.observability.health import check_component_health
from musubi.observability.logging_setup import (
    StructuredJsonFormatter,
    configure_logging,
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
from musubi.observability.tracing import (
    get_tracer,
    init_tracing,
    instrument_fastapi,
)
from musubi.observability.tracing import (
    is_enabled as tracing_is_enabled,
)

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "StructuredJsonFormatter",
    "check_component_health",
    "configure_logging",
    "default_registry",
    "get_tracer",
    "init_tracing",
    "install_metrics_middleware",
    "instrument_fastapi",
    "redact_token_filter",
    "render_text_format",
    "request_id_var",
    "tracing_is_enabled",
]
