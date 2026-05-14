"""Tests for :mod:`musubi.observability.tracing`.

Covers:

- Endpoint-gating: when ``endpoint`` is empty/None, every helper is a
  no-op.
- :func:`init_tracing` returning a :class:`TracerProvider` with the
  expected resource attributes when an endpoint is supplied.
- Idempotency — a second :func:`init_tracing` call does nothing.
- :func:`instrument_fastapi` no-op when tracing is disabled, instruments
  when enabled.
- :func:`get_tracer` always returns a tracer — production code can use
  ``tracer.start_as_current_span(...)`` unconditionally.

The OTLP exporter never actually talks to a network endpoint in these
tests: we stub it with an in-memory equivalent. End-to-end exporter
behaviour is covered by the OTel SDK's own test suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from musubi.observability import tracing


def _reset_otel_globals() -> None:
    """Reset OTel's process-wide TracerProvider + LoggingInstrumentor.

    OTel refuses to override its global TracerProvider once set; tests
    need a clean slate per case, so we reach into ``_TRACER_PROVIDER``
    and ``_TRACER_PROVIDER_SET_ONCE`` directly. These attributes are
    internal to OTel and not in its public type stubs; we use
    ``getattr``/``setattr`` to avoid mypy errors on the access.
    """
    import opentelemetry.trace as _otel_trace
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    tracing._reset_for_tests()
    setattr(_otel_trace, "_TRACER_PROVIDER", None)
    once_cls: Any = getattr(_otel_trace, "Once")
    setattr(_otel_trace, "_TRACER_PROVIDER_SET_ONCE", once_cls())
    if LoggingInstrumentor().is_instrumented_by_opentelemetry:
        LoggingInstrumentor().uninstrument()


@pytest.fixture(autouse=True)
def _reset_tracing_state() -> Iterator[None]:
    """Reset both Musubi's module-level singleton AND OTel's global
    TracerProvider between tests."""
    _reset_otel_globals()
    yield
    _reset_otel_globals()


@pytest.fixture(autouse=True)
def _stub_otlp_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the OTLP gRPC exporter with an in-memory one for tests.

    The real :class:`OTLPSpanExporter` spawns a background worker thread
    that tries to connect to the configured endpoint on first export.
    Tests don't need a live collector and the failed-connect retry
    noise floods test output. We wrap :class:`InMemorySpanExporter` so
    it accepts (and ignores) the ``endpoint=`` kwarg the real exporter
    requires.
    """
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    class _StubOTLPExporter(InMemorySpanExporter):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__()

    # The real import happens lazily inside init_tracing, so we have to
    # patch where the function reads it from.
    import opentelemetry.exporter.otlp.proto.grpc.trace_exporter as _otlp_mod

    monkeypatch.setattr(_otlp_mod, "OTLPSpanExporter", _StubOTLPExporter)


_ENDPOINT = "http://localhost:4317"


class TestIsEnabled:
    def test_disabled_when_endpoint_none(self) -> None:
        assert tracing.is_enabled(None) is False

    def test_disabled_when_endpoint_empty(self) -> None:
        assert tracing.is_enabled("") is False

    def test_disabled_when_endpoint_whitespace(self) -> None:
        assert tracing.is_enabled("   ") is False

    def test_enabled_when_endpoint_set(self) -> None:
        assert tracing.is_enabled("http://shiori.mey.house:4317") is True


class TestInitTracing:
    def test_returns_none_when_endpoint_unset(self) -> None:
        assert tracing.init_tracing(endpoint=None) is None

    def test_returns_provider_when_enabled(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider

        provider = tracing.init_tracing(
            endpoint=_ENDPOINT,
            host_name="testhost",
            service_version="0.0.0-test",
        )
        assert provider is not None
        assert isinstance(provider, TracerProvider)

    def test_resource_attributes_set_correctly(self) -> None:
        provider = tracing.init_tracing(
            endpoint=_ENDPOINT,
            service_name="musubi-core-test",
            service_namespace="musubi",
            host_name="testhost",
            service_version="v1.2.3",
            deployment_environment="test-env",
        )
        assert provider is not None
        attrs = provider.resource.attributes
        assert attrs["service.name"] == "musubi-core-test"
        assert attrs["service.namespace"] == "musubi"
        assert attrs["host.name"] == "testhost"
        assert attrs["service.version"] == "v1.2.3"
        assert attrs["deployment.environment"] == "test-env"

    def test_idempotent_second_call_returns_none(self) -> None:
        first = tracing.init_tracing(endpoint=_ENDPOINT, host_name="t")
        second = tracing.init_tracing(endpoint=_ENDPOINT, host_name="t")
        assert first is not None
        assert second is None

    def test_host_name_defaults_from_socket_when_unset(self) -> None:
        provider = tracing.init_tracing(endpoint=_ENDPOINT)
        assert provider is not None
        # Some hostname should be discovered — value depends on the
        # test host but should not be empty/None.
        assert provider.resource.attributes.get("host.name", "") != ""

    def test_service_version_omitted_when_blank(self) -> None:
        provider = tracing.init_tracing(endpoint=_ENDPOINT, host_name="t", service_version="")
        assert provider is not None
        # Empty string for version is treated as "not set" — the
        # resource attribute should be absent.
        assert "service.version" not in provider.resource.attributes


class TestInstrumentFastapi:
    def test_noop_when_tracing_disabled(self) -> None:
        from fastapi import FastAPI

        app = FastAPI()
        # Should not raise even though no provider is configured.
        tracing.instrument_fastapi(app)

    def test_instruments_app_when_enabled(self) -> None:
        from fastapi import FastAPI

        tracing.init_tracing(endpoint=_ENDPOINT, host_name="t")
        app = FastAPI()
        tracing.instrument_fastapi(app)
        # The FastAPIInstrumentor adds middleware as a side effect; no
        # public API to introspect that, but the call should be silent.
        # Confirming "no exception raised" is the contract here.


class TestGetTracer:
    def test_returns_tracer_when_disabled(self) -> None:
        t = tracing.get_tracer()
        # Even in no-op mode, start_as_current_span must work.
        with t.start_as_current_span("test.span"):
            pass

    def test_returns_tracer_when_enabled(self) -> None:
        tracing.init_tracing(endpoint=_ENDPOINT, host_name="t")
        t = tracing.get_tracer("musubi.test")
        with t.start_as_current_span("retrieve.dense_encode") as span:
            assert span.is_recording()


class TestLoggingInstrumentor:
    def test_init_tracing_installs_logging_instrumentor(self) -> None:
        """``init_tracing`` must install :class:`LoggingInstrumentor` so
        log records pick up the active span's ids.

        We assert the instrumentor was activated (the contract we own)
        rather than the trace-id payload on records (which is OTel's
        internal behaviour and fragile across test isolation).
        """
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        assert LoggingInstrumentor().is_instrumented_by_opentelemetry is False
        tracing.init_tracing(endpoint=_ENDPOINT, host_name="t")
        assert LoggingInstrumentor().is_instrumented_by_opentelemetry is True

    def test_disabled_does_not_install_logging_instrumentor(self) -> None:
        """When endpoint unset, LoggingInstrumentor stays uninstalled —
        no side effects on log records when tracing is off."""
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        tracing.init_tracing(endpoint=None)
        assert LoggingInstrumentor().is_instrumented_by_opentelemetry is False
