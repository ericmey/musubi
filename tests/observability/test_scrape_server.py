"""Test contract for `musubi.observability.scrape_server`.

Smoke-level: boot the server on an OS-picked port, make a real HTTP GET
against `/metrics`, assert 200 + Prometheus exposition shape. Then
register a counter, increment it, scrape again — confirm the value
shows up. This is enough to fail loudly if the server stops working
without depending on Prometheus being installed.
"""

from __future__ import annotations

import time
import urllib.request

import pytest

from musubi.observability.registry import default_registry
from musubi.observability.scrape_server import start_metrics_server


@pytest.fixture
def metrics_server() -> tuple[int, object]:
    """Boot the server on an OS-picked port; tear down after the test."""
    thread = start_metrics_server(port=0, host="127.0.0.1")
    httpd = thread.httpd  # type: ignore[attr-defined]
    port = httpd.server_address[1]
    # Tiny wait so the thread is past serve_forever() entry. The poll
    # loop below tolerates a slow boot, but a 1ms yield is enough to
    # avoid most flake.
    time.sleep(0.005)
    yield port, httpd
    httpd.shutdown()
    httpd.server_close()


def _get(port: int, path: str = "/metrics", timeout: float = 2.0) -> tuple[int, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8")


def test_metrics_endpoint_returns_200(metrics_server: tuple[int, object]) -> None:
    port, _ = metrics_server
    status, _body = _get(port)
    assert status == 200


def test_metrics_endpoint_renders_exposition_format(
    metrics_server: tuple[int, object],
) -> None:
    port, _ = metrics_server
    # Register + increment a counter so the body is non-trivial.
    reg = default_registry()
    counter = reg.counter(
        "musubi_scrape_server_test_total",
        "test counter for scrape_server endpoint smoke",
        labelnames=("variant",),
    )
    counter.labels(variant="alpha").inc(3)
    counter.labels(variant="beta").inc(1)

    status, body = _get(port)
    assert status == 200
    # Prometheus exposition format markers (# HELP, # TYPE).
    assert "# HELP musubi_scrape_server_test_total" in body
    assert "# TYPE musubi_scrape_server_test_total counter" in body
    # Both label-value combinations are rendered.
    assert 'musubi_scrape_server_test_total{variant="alpha"} 3' in body
    assert 'musubi_scrape_server_test_total{variant="beta"} 1' in body


def test_non_metrics_path_404s(metrics_server: tuple[int, object]) -> None:
    port, _ = metrics_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get(port, path="/")
    assert exc_info.value.code == 404
