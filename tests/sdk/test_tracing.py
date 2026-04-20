from __future__ import annotations

from unittest.mock import patch

import pytest

from musubi.sdk.tracing import HAS_OTEL, scrub_url, sdk_span


def test_scrub_url() -> None:
    assert scrub_url("http://user:pass@example.com/api?foo=bar") == "http://example.com/api?foo=bar"
    assert scrub_url("https://example.com/api") == "https://example.com/api"


def test_sdk_span_no_op_when_otel_missing() -> None:
    with patch("musubi.sdk.tracing.HAS_OTEL", False), sdk_span("op", "GET", "http://foo"):
        pass  # Does not raise error


@pytest.mark.skipif(not HAS_OTEL, reason="OTel not installed")
def test_sdk_span_records_attributes_when_otel_present() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    # We use patch on get_tracer because we don't want to mess up global state
    with patch("opentelemetry.trace.get_tracer", return_value=provider.get_tracer("test")):
        with sdk_span(
            "test_op", "GET", "http://example.com/foo", namespace="my-ns", request_id="123"
        ):
            pass

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.name == "musubi.test_op"
        assert span.attributes["http.method"] if span.attributes else None == "GET"
        assert span.attributes["http.url"] if span.attributes else None == "http://example.com/foo"
        assert span.attributes["musubi.namespace"] if span.attributes else None == "my-ns"
        assert span.attributes["musubi.request_id"] if span.attributes else None == "123"
        assert span.attributes and "musubi.duration_ms" in span.attributes
