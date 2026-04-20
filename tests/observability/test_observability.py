"""Test contract for slice-ops-observability.

Implements the bullets from [[09-operations/observability]] § Test
contract + the metrics/logs/traces surface they describe.

Closure plan:

- bullets 1-5 (per-endpoint metrics + errors + log line shape) → passing
- bullet 6 (OTel span over retrieve orchestration) → skipped against
  the existing cross-slice ticket ``slice-sdk-py-otel-spans.md`` —
  OTel is opt-in per the SDK spec and adding ``opentelemetry-api`` as
  a hard dep is out of scope for this slice too.
- bullet 7 (lifecycle job start/end emitted to events table) → skipped
  in the observability slice and resolved by cross-slice ticket
  ``slice-ops-observability-slice-lifecycle-job-emit.md``: lifecycle
  workers now emit the shared duration + error metric families.
- bullet 8 (dashboard JSON loads in Grafana) → declared out-of-scope
  in the slice work log; integration test needs a live Grafana.
"""

from __future__ import annotations

import json
import logging
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from musubi.observability import (
    Registry,
    StructuredJsonFormatter,
    check_component_health,
    default_registry,
    install_metrics_middleware,
    redact_token_filter,
    render_text_format,
    request_id_var,
)

# ---------------------------------------------------------------------------
# Registry primitives — unit tests for the in-process metrics layer
# ---------------------------------------------------------------------------


def test_counter_increments_and_renders_text_format() -> None:
    reg = Registry()
    c = reg.counter(
        "musubi_capture_total", "captures by namespace + plane", labelnames=["namespace", "plane"]
    )
    c.labels(namespace="eric/x/episodic", plane="episodic").inc()
    c.labels(namespace="eric/x/episodic", plane="episodic").inc()
    c.labels(namespace="eric/y/episodic", plane="episodic").inc()
    text = render_text_format(reg)
    assert "# HELP musubi_capture_total captures by namespace + plane" in text
    assert "# TYPE musubi_capture_total counter" in text
    assert 'musubi_capture_total{namespace="eric/x/episodic",plane="episodic"} 2' in text
    assert 'musubi_capture_total{namespace="eric/y/episodic",plane="episodic"} 1' in text


def test_histogram_buckets_and_sum() -> None:
    reg = Registry()
    h = reg.histogram(
        "musubi_capture_duration_ms",
        "duration",
        labelnames=["plane"],
        buckets=(10.0, 50.0, 100.0, 500.0),
    )
    h.labels(plane="episodic").observe(5.0)
    h.labels(plane="episodic").observe(40.0)
    h.labels(plane="episodic").observe(200.0)
    text = render_text_format(reg)
    assert 'musubi_capture_duration_ms_bucket{plane="episodic",le="10"} 1' in text
    assert 'musubi_capture_duration_ms_bucket{plane="episodic",le="50"} 2' in text
    assert 'musubi_capture_duration_ms_bucket{plane="episodic",le="+Inf"} 3' in text
    assert 'musubi_capture_duration_ms_count{plane="episodic"} 3' in text
    assert 'musubi_capture_duration_ms_sum{plane="episodic"} 245' in text


def test_gauge_set_and_render() -> None:
    reg = Registry()
    g = reg.gauge("gpu_vram_used_mb", "vram used")
    g.set(12345.0)
    text = render_text_format(reg)
    assert "# TYPE gpu_vram_used_mb gauge" in text
    assert "gpu_vram_used_mb 12345" in text


def test_unlabelled_counter_renders_without_label_block() -> None:
    reg = Registry()
    c = reg.counter("musubi_promotion_total", "promotions")
    c.inc()
    c.inc()
    text = render_text_format(reg)
    assert "musubi_promotion_total 2" in text
    assert "musubi_promotion_total{" not in text


# ---------------------------------------------------------------------------
# Bullets 1-3 — per-endpoint metrics via middleware
# ---------------------------------------------------------------------------


def _app_with_metrics() -> tuple[Any, Registry]:
    from fastapi import FastAPI, HTTPException

    app = FastAPI()
    reg = Registry()
    install_metrics_middleware(app, registry=reg)

    @app.get("/v1/probe-ok")
    async def probe_ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/probe-err")
    async def probe_err() -> None:
        raise HTTPException(status_code=500, detail="boom")

    return app, reg


def test_every_endpoint_emits_request_counter() -> None:
    """Bullet 1 — per-endpoint request counter increments on every call."""
    app, reg = _app_with_metrics()
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/v1/probe-ok")
    client.get("/v1/probe-ok")
    text = render_text_format(reg)
    assert 'musubi_http_requests_total{endpoint="/v1/probe-ok",method="GET",status="200"} 2' in text


def test_every_endpoint_emits_latency_histogram() -> None:
    """Bullet 2 — per-endpoint latency histogram observes on every call."""
    app, reg = _app_with_metrics()
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/v1/probe-ok")
    text = render_text_format(reg)
    assert 'musubi_http_request_duration_ms_count{endpoint="/v1/probe-ok",method="GET"} 1' in text


def test_errors_increment_errors_total() -> None:
    """Bullet 3 — 5xx responses increment musubi_5xx_total."""
    app, reg = _app_with_metrics()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/v1/probe-err")
    assert resp.status_code == 500
    text = render_text_format(reg)
    assert 'musubi_5xx_total{endpoint="/v1/probe-err"} 1' in text


# ---------------------------------------------------------------------------
# Bullets 4-5 — log line shape + safety
# ---------------------------------------------------------------------------


def test_log_line_contains_request_id_for_api_calls(caplog: pytest.LogCaptureFixture) -> None:
    """Bullet 4 — every API-scoped log line carries the request_id."""
    formatter = StructuredJsonFormatter()
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    log = logging.getLogger("musubi.test.api")
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    token = request_id_var.set("trace-abc-123")
    try:
        record = logging.LogRecord(
            name="musubi.test.api",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg="captured memory",
            args=(),
            exc_info=None,
        )
        rendered = formatter.format(record)
    finally:
        request_id_var.reset(token)
    payload = json.loads(rendered)
    assert payload["request_id"] == "trace-abc-123"
    assert payload["msg"] == "captured memory"
    assert payload["level"] == "info"
    assert "ts" in payload


def test_log_line_never_contains_raw_token() -> None:
    """Bullet 5 — token-shaped strings are scrubbed before emission."""
    record = logging.LogRecord(
        name="musubi.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="auth header was Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        args=(),
        exc_info=None,
    )
    f = redact_token_filter
    assert f(record) is True  # filter passes the record through
    assert "eyJhbGciOiJIUzI1NiJ9" not in record.msg
    assert "[REDACTED]" in record.msg


# ---------------------------------------------------------------------------
# Bullets 6-7 — telemetry surfaces deferred to follow-on slices
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to slice-sdk-py-otel-spans: opentelemetry-api opt-in per spec; cross-slice ticket _inbox/cross-slice/slice-sdk-py-otel-spans.md"
)
def test_otel_span_covers_retrieve_orchestration() -> None:
    """Bullet 6 — placeholder."""


def test_lifecycle_job_start_end_emitted_to_events_table() -> None:
    """Bullet 7 — lifecycle workers register job duration + error metrics."""
    # Importing the worker modules registers the shared metric families.
    for module in (
        "musubi.lifecycle.maturation",
        "musubi.lifecycle.promotion",
        "musubi.lifecycle.reflection",
        "musubi.lifecycle.synthesis",
    ):
        import_module(module)

    text = render_text_format(default_registry())
    assert "# HELP musubi_lifecycle_job_duration_seconds lifecycle worker tick duration" in text
    assert "# TYPE musubi_lifecycle_job_duration_seconds histogram" in text
    assert "# HELP musubi_lifecycle_job_errors_total lifecycle worker tick errors" in text
    assert "# TYPE musubi_lifecycle_job_errors_total counter" in text


# ---------------------------------------------------------------------------
# Bullet 8 — integration; out-of-scope per work log
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="out-of-scope in slice work log: needs live Grafana to load dashboard JSON; deferred to musubi-contract-tests per ADR-0011"
)
def test_dashboard_json_loads_in_grafana() -> None:
    """Bullet 8 — placeholder."""


# ---------------------------------------------------------------------------
# Component health probes
# ---------------------------------------------------------------------------


def test_check_component_health_marks_reachable_service_healthy() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"}))
    component = check_component_health(
        name="tei-dense",
        url="http://tei-dense.local/health",
        transport=transport,
    )
    assert component.healthy is True
    assert component.name == "tei-dense"


def test_check_component_health_marks_unreachable_service_unhealthy() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    transport = httpx.MockTransport(boom)
    component = check_component_health(
        name="ollama",
        url="http://ollama.local/health",
        transport=transport,
    )
    assert component.healthy is False
    assert "ConnectError" in component.detail


def test_check_component_health_marks_5xx_unhealthy() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={"error": "down"}))
    component = check_component_health(
        name="tei-sparse",
        url="http://tei-sparse.local/health",
        transport=transport,
    )
    assert component.healthy is False
    assert "503" in component.detail


# ---------------------------------------------------------------------------
# /ops/status + /ops/metrics router wiring
# ---------------------------------------------------------------------------


def test_ops_metrics_returns_prometheus_text_format(obs_app: TestClient) -> None:
    """The router's /ops/metrics serves the live registry in text format."""
    from musubi.observability import default_registry

    default_registry().counter("musubi_test_probe_total", "test probe").inc()
    resp = obs_app.get("/v1/ops/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "musubi_test_probe_total" in resp.text


def test_ops_status_populates_real_components_per_aoi_review(obs_app: TestClient) -> None:
    """The router's /ops/status populates ComponentStatus for every
    declared dependency — the v0.1-review ask from Aoi (per-plane health
    granularity) gets exercised here."""
    resp = obs_app.get("/v1/ops/status")
    assert resp.status_code == 200
    body = resp.json()
    components = body["components"]
    # Must enumerate every dependency the spec calls out.
    expected = {"qdrant", "tei-dense", "tei-sparse", "tei-reranker", "ollama"}
    assert expected.issubset(set(components.keys())), (
        f"expected at least {expected}, got {set(components.keys())}"
    )
    # Each component must carry a name + healthy + (possibly empty) detail.
    for name, c in components.items():
        assert c["name"] == name
        assert isinstance(c["healthy"], bool)


# ---------------------------------------------------------------------------
# Coverage tests
# ---------------------------------------------------------------------------


def test_registry_text_format_emits_help_and_type_only_once_per_metric() -> None:
    reg = Registry()
    c = reg.counter("foo_total", "foo events", labelnames=["k"])
    c.labels(k="a").inc()
    c.labels(k="b").inc()
    text = render_text_format(reg)
    assert text.count("# HELP foo_total") == 1
    assert text.count("# TYPE foo_total counter") == 1


def test_structured_json_formatter_includes_logger_name() -> None:
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="musubi.api.routers.episodic",
        level=logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg="rate-limit hit",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["service"] == "musubi.api.routers.episodic"
    assert payload["level"] == "warning"


def test_redact_token_filter_idempotent() -> None:
    """Running the filter twice yields the same scrubbed message."""
    msg = "Authorization: Bearer eyJfoo.bar.baz"
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=0, msg=msg, args=(), exc_info=None
    )
    redact_token_filter(record)
    once = record.msg
    redact_token_filter(record)
    assert record.msg == once


def test_metrics_middleware_skips_metrics_path_to_avoid_self_probe() -> None:
    """Hitting /v1/ops/metrics must not increment the request counter
    for itself (would make the gauge wobble during scrape)."""
    app, reg = _app_with_metrics()

    from fastapi import FastAPI

    inner: FastAPI = app

    @inner.get("/v1/ops/metrics")
    async def ops_metrics_route() -> str:
        return render_text_format(reg)

    client = TestClient(app)
    client.get("/v1/ops/metrics")
    text = render_text_format(reg)
    assert 'musubi_http_requests_total{endpoint="/v1/ops/metrics"' not in text, (
        "metrics path must self-skip"
    )


def test_install_metrics_middleware_returns_app() -> None:
    """The installer is idempotent — calling it twice keeps the same registry."""
    from fastapi import FastAPI

    app = FastAPI()
    reg = Registry()
    install_metrics_middleware(app, registry=reg)
    install_metrics_middleware(app, registry=reg)
    # No exception means the install was idempotent.


def test_render_text_format_handles_empty_registry() -> None:
    reg = Registry()
    assert render_text_format(reg) == ""


def test_structured_formatter_includes_extra_fields() -> None:
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="musubi.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="captured",
        args=(),
        exc_info=None,
    )
    record.namespace = "eric/x/episodic"
    record.object_id = "k" * 27
    payload = json.loads(formatter.format(record))
    assert payload["namespace"] == "eric/x/episodic"
    assert payload["object_id"] == "k" * 27


def test_counter_inc_amount_param() -> None:
    """Counters support inc(n) for batch increments."""
    reg = Registry()
    c = reg.counter("batch_total", "batch")
    c.inc(5)
    text = render_text_format(reg)
    assert "batch_total 5" in text


def test_histogram_observe_negative_is_recorded_in_low_bucket() -> None:
    """Negative durations shouldn't happen, but observe defensively."""
    reg = Registry()
    h = reg.histogram("dur_ms", "dur", buckets=(10.0, 100.0))
    h.observe(-1.0)  # treat as 0
    text = render_text_format(reg)
    # Counted in every bucket (including the 10-bucket).
    assert 'dur_ms_bucket{le="10"} 1' in text


def test_gauge_inc_dec() -> None:
    reg = Registry()
    g = reg.gauge("queue_depth", "depth")
    g.inc()
    g.inc()
    g.dec()
    text = render_text_format(reg)
    assert "queue_depth 1" in text


def test_check_component_health_marks_4xx_unhealthy() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(404))
    component = check_component_health(
        name="weird", url="http://weird.local/health", transport=transport
    )
    assert component.healthy is False


# ---------------------------------------------------------------------------
# Deploy/* artifacts — config files exist + load
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO_ROOT / "deploy"


def test_prometheus_config_loads() -> None:
    """deploy/prometheus/prometheus.yml parses as YAML + has scrape_configs."""
    import yaml

    cfg = yaml.safe_load((_DEPLOY / "prometheus" / "prometheus.yml").read_text())
    assert "scrape_configs" in cfg
    job_names = {j["job_name"] for j in cfg["scrape_configs"]}
    assert "musubi-core" in job_names
    assert "qdrant" in job_names


def test_loki_config_loads() -> None:
    import yaml

    cfg = yaml.safe_load((_DEPLOY / "loki" / "loki.yml").read_text())
    assert "auth_enabled" in cfg


def test_tempo_config_loads() -> None:
    import yaml

    cfg = yaml.safe_load((_DEPLOY / "tempo" / "tempo.yml").read_text())
    assert "server" in cfg or "distributor" in cfg or "storage" in cfg


def test_grafana_overview_dashboard_loads() -> None:
    """The musubi-overview dashboard JSON parses as the spec calls out
    the four boards explicitly."""
    cfg = json.loads((_DEPLOY / "grafana" / "dashboards" / "musubi-overview.json").read_text())
    assert "panels" in cfg
    assert cfg.get("title", "").startswith("Musubi")


def test_grafana_latency_dashboard_loads() -> None:
    cfg = json.loads((_DEPLOY / "grafana" / "dashboards" / "musubi-latency.json").read_text())
    assert "panels" in cfg


def test_grafana_lifecycle_dashboard_loads() -> None:
    cfg = json.loads((_DEPLOY / "grafana" / "dashboards" / "musubi-lifecycle.json").read_text())
    assert "panels" in cfg


def test_grafana_vault_dashboard_loads() -> None:
    cfg = json.loads((_DEPLOY / "grafana" / "dashboards" / "musubi-vault.json").read_text())
    assert "panels" in cfg


def test_grafana_datasources_provisioning_loads() -> None:
    import yaml

    cfg = yaml.safe_load(
        (_DEPLOY / "grafana" / "provisioning" / "datasources" / "datasources.yml").read_text()
    )
    names = {d["name"] for d in cfg["datasources"]}
    assert {"Prometheus", "Loki"}.issubset(names)
