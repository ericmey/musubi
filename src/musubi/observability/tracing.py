"""OpenTelemetry tracing setup for Musubi Core.

Per [[09-operations/observability]] § Tracing — completes the Tracing
portion of `slice-ops-observability` that was scoped but never shipped
(see issue #302 and the 2026-05-13 work-log entry on that slice).

Behaviour:

- Endpoint-gated. If ``endpoint`` is empty / ``None``, every helper here
  is a no-op (returns ``None`` / does nothing). The server runs
  unchanged when traces are not wanted.
- When ``endpoint`` is provided, :func:`init_tracing` builds a
  :class:`TracerProvider` with a :class:`Resource` carrying the
  ``service.name``, ``service.namespace``, ``host.name``,
  ``service.version``, and ``deployment.environment`` attributes that
  the rest of the fleet emits (so musubi-core lines up with openclaw,
  livekit, etc. on Tempo + Mimir labels).
- Span export goes over OTLP/gRPC to the supplied endpoint
  (e.g. ``http://shiori.mey.house:4317``).
- 100% sampling per the spec ("100% in v1 (low traffic; dedicated host
  has spare headroom)"). No sampler argument is passed; OTel's default
  is ``ParentBased(AlwaysOn)`` which is exactly 100% root-sampling.
- :class:`LoggingInstrumentor` is installed alongside the tracer so that
  ``trace_id`` / ``span_id`` are exposed on log records, picked up by
  :class:`musubi.observability.logging_setup.StructuredJsonFormatter`.

This module does not read environment variables directly. Per the
codebase rule enforced by
``tests/test_config.py::test_no_module_imports_os_environ_for_config``,
environment access lives in :mod:`musubi.config` /
:mod:`musubi.settings` only. Callers — typically
:func:`musubi.api.app.create_app` — pass values pulled from
:class:`Settings`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Tracer

log = logging.getLogger(__name__)

# Module-level singleton so init_tracing() is idempotent. Tests can reset
# this via _reset_for_tests().
_provider: TracerProvider | None = None
_provider_initialized: bool = False


def is_enabled(endpoint: str | None) -> bool:
    """Return True when an OTLP endpoint is configured.

    Callers should pass ``settings.otel_exporter_otlp_endpoint``. An
    empty string or ``None`` disables tracing.
    """
    return bool(endpoint and endpoint.strip())


def init_tracing(
    *,
    endpoint: str | None,
    service_name: str = "musubi-core",
    service_namespace: str = "musubi",
    host_name: str | None = None,
    service_version: str | None = None,
    deployment_environment: str = "harem-world",
) -> TracerProvider | None:
    """Build and install the global TracerProvider.

    Returns the provider on success, ``None`` when tracing is disabled
    (no endpoint supplied) or when called more than once.

    Idempotent: a second call returns ``None`` and does not reinstall.

    All values are passed by the caller — typically pulled from
    :class:`musubi.settings.Settings`. Defaults match what the rest of
    the fleet emits so musubi-core's spans align on the same labels as
    openclaw/livekit in Tempo + Mimir.
    """
    global _provider, _provider_initialized

    # Idempotency: only short-circuit when a provider was actually
    # installed previously. No-op paths (endpoint unset, ImportError)
    # leave the flag unset so a later call with a real endpoint /
    # available OTel install can still initialize. Copilot review on
    # PR #303 flagged the original eager-flag-set as a real bug.
    if _provider_initialized:
        return None

    if not is_enabled(endpoint):
        log.debug("tracing.init: no endpoint provided; tracing disabled")
        return None

    # Defer all OTel SDK imports until we know we need them. Keeps the
    # module importable even when the [otel] extra is not installed.
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        log.warning(
            "tracing.init: OTel SDK not installed; tracing disabled "
            "(install with `pip install musubi[otel]`). Detail: %s",
            exc,
        )
        return None

    resource_attrs: dict[str, str] = {
        "service.name": service_name,
        "service.namespace": service_namespace,
        "deployment.environment": deployment_environment,
    }
    if host_name is None:
        host_name = _read_hostname()
    if host_name:
        resource_attrs["host.name"] = host_name
    if service_version:
        resource_attrs["service.version"] = service_version

    resource = Resource.create(resource_attrs)
    provider = TracerProvider(resource=resource)
    # The supplied endpoint is the OTLP/gRPC target; the exporter does
    # not also need to read OTEL_EXPORTER_OTLP_ENDPOINT.
    assert endpoint is not None  # narrowed by is_enabled() above
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Inject trace_id / span_id into log records so the formatter can
    # serialize them. This is what gives us logs ↔ traces correlation.
    LoggingInstrumentor().instrument(set_logging_format=False)

    _provider = provider
    _provider_initialized = True  # only after the install actually succeeded
    log.info(
        "tracing.init: ok (service=%s namespace=%s endpoint=%s)",
        service_name,
        service_namespace,
        endpoint,
    )
    return provider


def instrument_fastapi(app: FastAPI) -> None:
    """Install FastAPI auto-instrumentation if tracing is enabled.

    Safe to call when tracing is disabled — no-op then.
    """
    if _provider is None:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        log.warning("tracing.instrument_fastapi: OTel FastAPI instrumentation not installed")
        return
    FastAPIInstrumentor.instrument_app(app)
    log.debug("tracing.instrument_fastapi: instrumented FastAPI app")


class _NoOpSpan:
    """Local no-op span used when ``opentelemetry-api`` is not installed.

    Implements just the subset of the Span API hand-instrumented spans
    in this codebase actually call:

    - context-manager (``__enter__``/``__exit__``) so
      ``with tracer.start_as_current_span(...) as span:`` works.
    - ``set_attribute`` so attribute-tagging calls are silently dropped.
    - ``is_recording`` returns ``False`` for tests that want to check
      whether the span is real.
    """

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def set_attribute(self, key: str, value: object) -> None:
        return None

    def is_recording(self) -> bool:
        return False


class _NoOpTracer:
    """Local no-op tracer for the ``opentelemetry-api``-not-installed path.

    Only ``start_as_current_span`` is exposed because that's the surface
    every caller in this codebase uses. If more API is needed later,
    extend here — we'd rather extend a small local class than make
    ``opentelemetry-api`` a hard dependency.
    """

    def start_as_current_span(self, _name: str, **_kwargs: object) -> _NoOpSpan:
        return _NoOpSpan()


def get_tracer(name: str = "musubi") -> Tracer:
    """Return an OTel tracer for hand-instrumented spans.

    When tracing is disabled or ``opentelemetry-api`` is not installed,
    returns a local no-op tracer; callers can use
    ``tracer.start_as_current_span(...)`` unconditionally without
    runtime guards. The local fallback matters because callers like
    :mod:`musubi.retrieve.orchestration` invoke ``get_tracer()`` at
    module-import time — a hard ``ImportError`` here would crash the
    process on a default ``pip install musubi`` (no ``[otel]`` extra).
    """
    try:
        from opentelemetry import trace as _trace  # local import keeps cold start cheap
    except ImportError:
        return _NoOpTracer()  # type: ignore[return-value]

    return _trace.get_tracer(name)


def _read_hostname() -> str | None:
    """Read the host's name from socket.gethostname()."""
    try:
        import socket

        return socket.gethostname() or None
    except OSError:
        return None


def _reset_for_tests() -> None:
    """Reset module state. Test-only — production code never calls this."""
    global _provider, _provider_initialized
    _provider = None
    _provider_initialized = False
