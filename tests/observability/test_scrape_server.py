"""Test contract for `musubi.observability.scrape_server`.

Smoke-level: boot the server on an OS-picked port, make a real HTTP GET
against `/metrics`, assert 200 + Prometheus exposition shape. Then
register a counter, increment it, scrape again — confirm the value
shows up. This is enough to fail loudly if the server stops working
without depending on Prometheus being installed.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from musubi.observability.registry import default_registry
from musubi.observability.scrape_server import start_metrics_server


@pytest.fixture
def metrics_server() -> Iterator[tuple[int, object]]:
    """Boot the server on an OS-picked port; tear down after the test."""
    thread = start_metrics_server(port=0, host="127.0.0.1")
    httpd = thread.httpd  # type: ignore[attr-defined]
    port = httpd.server_address[1]
    # Poll until the server is actually accepting connections. The
    # thread is `start()`ed before the fixture sees it, but there is a
    # brief window before `serve_forever()` enters its loop. Try a
    # couple of GETs with short retry delays rather than a single
    # arbitrary sleep — the alternative is flaky on slow runners.
    deadline = time.monotonic() + 1.0
    boot_confirmed = False
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=0.2).close()
            boot_confirmed = True
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.02)
    if not boot_confirmed:
        # Fall-through means the deadline elapsed without a successful
        # GET — turn that into a clear failure here rather than letting
        # the actual test fail later with a confusing connection error.
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)
        pytest.fail(
            f"metrics-server did not start accepting connections on 127.0.0.1:{port} within 1s"
        )
    yield port, httpd
    httpd.shutdown()
    httpd.server_close()
    # Join the server thread so we don't leak it across tests. The
    # daemon flag stops process shutdown from blocking; an explicit
    # join here stops a slow-shutting-down server from accumulating
    # idle threads in long test runs.
    thread.join(timeout=2.0)


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
    # Per-test unique metric name so test reruns + future tests that
    # reuse the global `default_registry()` singleton don't end up
    # asserting against a counter that's already been incremented by
    # a prior test in the same session.
    metric_name = f"musubi_scrape_server_test_{time.monotonic_ns()}_total"
    reg = default_registry()
    counter = reg.counter(
        metric_name,
        "test counter for scrape_server endpoint smoke",
        labelnames=("variant",),
    )
    counter.labels(variant="alpha").inc(3)
    counter.labels(variant="beta").inc(1)

    status, body = _get(port)
    assert status == 200
    # Prometheus exposition format markers (# HELP, # TYPE).
    assert f"# HELP {metric_name}" in body
    assert f"# TYPE {metric_name} counter" in body
    # Both label-value combinations are rendered.
    assert f'{metric_name}{{variant="alpha"}} 3' in body
    assert f'{metric_name}{{variant="beta"}} 1' in body


def test_non_metrics_path_404s(metrics_server: tuple[int, object]) -> None:
    port, _ = metrics_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        _get(port, path="/")
    assert exc_info.value.code == 404
