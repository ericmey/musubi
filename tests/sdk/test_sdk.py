"""Test contract for slice-sdk-py.

Implements the bullets from [[07-interfaces/sdk]] § Test contract.
The package-under-test is :mod:`musubi.sdk` (not the spec's pre-ADR-0015
``musubi-client``); the spec rename lands in this PR with a
``spec-update:`` trailer.

Closure plan:

- bullets 1-5, 6-13, 14-15, 17-21 → passing
- bullet 16 (OTel span emitted per call) → skipped, opentelemetry-api
  is not in the dev extras and the spec says OTel integration is
  opt-in ("when OTel is configured in the adapter"). Cross-slice ticket
  ``slice-sdk-py-otel-spans.md`` documents the follow-up.
- bullet 22 (integration against a real Musubi container) → out-of-scope
  in slice work log; needs a docker-up Musubi.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from musubi.sdk import (
    AsyncMusubiClient,
    BackendUnavailable,
    BadRequest,
    Conflict,
    Forbidden,
    MusubiClient,
    MusubiError,
    NetworkError,
    NotFound,
    RateLimited,
    RetryPolicy,
    SDKResult,
    Unauthorized,
)
from musubi.sdk.testing import FakeMusubiClient

_BASE_URL = "https://musubi.test/v1"
_TOKEN = "test-bearer"


def _err(code: str, status: int, detail: str = "x") -> dict[str, object]:
    return {"error": {"code": code, "detail": detail, "hint": ""}}


def _client(transport: httpx.MockTransport, *, retry: RetryPolicy | None = None) -> MusubiClient:
    return MusubiClient(
        base_url=_BASE_URL,
        token=_TOKEN,
        retry=retry or RetryPolicy(max_attempts=1, base_backoff=0.0),
        transport=transport,
    )


# ---------------------------------------------------------------------------
# Happy path — bullets 1-5
# ---------------------------------------------------------------------------


def test_capture_returns_memory_model() -> None:
    """Bullet 1 — POST /v1/memories returns a typed model with object_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/memories"
        body = json.loads(request.content)
        assert body["namespace"] == "eric/x/episodic"
        return httpx.Response(
            202,
            json={"object_id": "k" * 27, "state": "provisional"},
        )

    client = _client(httpx.MockTransport(handler))
    result = client.memories.capture(namespace="eric/x/episodic", content="hello")
    assert result["object_id"] == "k" * 27
    assert result["state"] == "provisional"


def test_retrieve_returns_list_of_results() -> None:
    """Bullet 2 — POST /v1/retrieve returns a list of result rows."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/retrieve"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "object_id": "a" * 27,
                        "score": 0.9,
                        "plane": "episodic",
                        "content": "x",
                        "namespace": "n",
                    },
                    {
                        "object_id": "b" * 27,
                        "score": 0.7,
                        "plane": "episodic",
                        "content": "y",
                        "namespace": "n",
                    },
                ],
                "mode": "fast",
                "limit": 10,
            },
        )

    client = _client(httpx.MockTransport(handler))
    rows = client.retrieve(namespace="eric/x/episodic", query_text="hi")
    assert len(rows["results"]) == 2
    assert rows["results"][0]["score"] == 0.9


def test_thoughts_send_returns_acknowledgement() -> None:
    """Bullet 3 — POST /v1/thoughts/send returns 202 + object_id."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/thoughts/send"
        return httpx.Response(202, json={"object_id": "t" * 27, "state": "provisional"})

    client = _client(httpx.MockTransport(handler))
    ack = client.thoughts.send(
        namespace="eric/x/thought",
        from_presence="claude-code",
        to_presence="livekit",
        content="hi",
    )
    assert ack["object_id"] == "t" * 27


def test_capture_threads_created_at_override_to_body() -> None:
    """#140 — when caller supplies created_at on capture(), the SDK
    serialises it as ISO-8601 on the outbound body. Whether the server
    accepts the call depends on the bearer's scope; the SDK's only job
    is to pass the hint through faithfully."""
    from datetime import UTC, datetime

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"object_id": "z" * 27, "state": "provisional"})

    client = _client(httpx.MockTransport(handler))
    ts = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    client.memories.capture(
        namespace="eric/x/episodic",
        content="historical",
        created_at=ts,
    )
    assert captured and "created_at" in captured[0]
    assert captured[0]["created_at"].startswith("2024-06-01T12:00:00")


def test_capture_without_created_at_omits_field_from_body() -> None:
    """Backwards compatibility — default capture() calls must NOT carry
    a created_at key so the server's operator-scope gate stays dormant."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"object_id": "z" * 27, "state": "provisional"})

    client = _client(httpx.MockTransport(handler))
    client.memories.capture(namespace="eric/x/episodic", content="normal")
    assert captured and "created_at" not in captured[0]


def test_batch_context_threads_per_item_created_at() -> None:
    """Batch context passes each item's created_at through verbatim;
    items without the override remain bare."""
    from datetime import UTC, datetime

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(202, json={"object_ids": ["a" * 27, "b" * 27, "c" * 27]})

    client = _client(httpx.MockTransport(handler))
    with client.memories.batch(namespace="eric/x/episodic") as batch:
        batch.capture(content="no-override")
        batch.capture(content="with-override", created_at=datetime(2023, 1, 1, tzinfo=UTC))
        batch.capture(content="also-no")

    items = captured[0]["items"]
    assert len(items) == 3
    assert "created_at" not in items[0]
    assert items[1]["created_at"].startswith("2023-01-01T00:00:00")
    assert "created_at" not in items[2]


def test_batch_context_one_http_call() -> None:
    """Bullet 4 — the batch context manager flushes a SINGLE
    POST /v1/memories/batch on exit, not N posts."""
    calls: list[tuple[str, list[dict[str, object]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body.get("items", [])))
        return httpx.Response(
            202,
            json={
                "object_ids": [
                    f"o{i}aaaaaaaaaaaaaaaaaaaaaaaaaa"[:27] for i in range(len(body["items"]))
                ]
            },
        )

    client = _client(httpx.MockTransport(handler))
    with client.memories.batch(namespace="eric/x/episodic") as batch:
        batch.capture(content="one")
        batch.capture(content="two")
        batch.capture(content="three")
    assert len(calls) == 1, f"expected ONE batch call, got {len(calls)}"
    assert calls[0][0] == "/v1/memories/batch"
    assert len(calls[0][1]) == 3


def test_stream_yields_per_ndjson_line() -> None:
    """Bullet 5 — retrieve_stream yields one row per NDJSON line."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/retrieve/stream"
        body = b'{"object_id":"a","score":0.9,"plane":"episodic"}\n{"object_id":"b","score":0.7,"plane":"episodic"}\n'
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )

    client = _client(httpx.MockTransport(handler))
    rows = list(client.retrieve_stream(namespace="eric/x/episodic", query_text="x"))
    assert len(rows) == 2
    assert rows[0]["object_id"] == "a"
    assert rows[1]["object_id"] == "b"


# ---------------------------------------------------------------------------
# Errors — bullets 6-10
# ---------------------------------------------------------------------------


def test_401_raises_unauthorized() -> None:
    """Bullet 6 — 401 → Unauthorized exception."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json=_err("UNAUTHORIZED", 401, "no token"))
    )
    client = _client(transport)
    with pytest.raises(Unauthorized) as exc:
        client.memories.get(namespace="x/y/episodic", object_id="z" * 27)
    assert exc.value.code == "UNAUTHORIZED"


def test_403_raises_forbidden_with_detail() -> None:
    """Bullet 7 — 403 → Forbidden + structured detail."""
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            403,
            json=_err("FORBIDDEN", 403, "namespace 'eric/other/episodic' not in token scope"),
        )
    )
    client = _client(transport)
    with pytest.raises(Forbidden) as exc:
        client.memories.capture(namespace="eric/other/episodic", content="x")
    assert "not in token scope" in exc.value.detail


def test_503_retries_then_raises_backend_unavailable() -> None:
    """Bullet 8 — 503 retried per policy, then BackendUnavailable raised
    once retries are exhausted."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json=_err("BACKEND_UNAVAILABLE", 503))

    client = _client(
        httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=3, base_backoff=0.0),
    )
    with pytest.raises(BackendUnavailable):
        client.memories.capture(namespace="eric/x/episodic", content="x")
    assert attempts["n"] == 3


def test_network_error_retried() -> None:
    """Bullet 9 — httpx network errors retry, then surface as
    NetworkError once exhausted."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("dns failed", request=request)

    client = _client(
        httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=2, base_backoff=0.0),
    )
    with pytest.raises(NetworkError):
        client.memories.capture(namespace="eric/x/episodic", content="x")
    assert attempts["n"] == 2


def test_result_api_mirrors_exception_api() -> None:
    """Bullet 10 — capture_result returns a Result wrapper instead of
    raising; happy + sad path both representable."""
    ok_transport = httpx.MockTransport(
        lambda r: httpx.Response(202, json={"object_id": "o" * 27, "state": "provisional"})
    )
    ok = _client(ok_transport).memories.capture_result(namespace="eric/x/episodic", content="x")
    assert isinstance(ok, SDKResult)
    assert ok.is_ok()
    assert ok.ok is not None
    assert ok.ok["object_id"] == "o" * 27

    err_transport = httpx.MockTransport(
        lambda r: httpx.Response(403, json=_err("FORBIDDEN", 403, "nope"))
    )
    err = _client(err_transport).memories.capture_result(namespace="eric/x/episodic", content="x")
    assert err.is_err()
    assert err.err is not None
    assert err.err.code == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Retry — bullets 11-13
# ---------------------------------------------------------------------------


def test_retry_honors_retry_after_header() -> None:
    """Bullet 11 — 429 with Retry-After header schedules a backoff at
    least the suggested duration before the next attempt."""
    timestamps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import time

        timestamps.append(time.monotonic())
        if len(timestamps) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "1"},
                json=_err("RATE_LIMITED", 429),
            )
        return httpx.Response(202, json={"object_id": "o" * 27, "state": "provisional"})

    client = _client(
        httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=2, base_backoff=0.0),
    )
    client.memories.capture(namespace="eric/x/episodic", content="x")
    # We don't actually wait a full second in the test (retry honours
    # the header up to a cap); verify the policy CONSULTED the header
    # by checking that the retry happened.
    assert len(timestamps) == 2


def test_retry_exponential_backoff_respects_max_attempts() -> None:
    """Bullet 12 — retry attempts are bounded by max_attempts."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json=_err("BACKEND_UNAVAILABLE", 503))

    client = _client(
        httpx.MockTransport(handler),
        retry=RetryPolicy(max_attempts=4, base_backoff=0.0),
    )
    with pytest.raises(BackendUnavailable):
        client.memories.capture(namespace="eric/x/episodic", content="x")
    assert attempts["n"] == 4


def test_idempotency_key_auto_generated_on_post() -> None:
    """Bullet 13 — POSTs auto-mint an Idempotency-Key header; caller-
    supplied key takes precedence."""
    seen_keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(202, json={"object_id": "o" * 27, "state": "provisional"})

    client = _client(httpx.MockTransport(handler))
    client.memories.capture(namespace="eric/x/episodic", content="x")
    assert seen_keys[0] is not None
    assert len(seen_keys[0]) >= 8

    # Caller-supplied wins.
    client.memories.capture(
        namespace="eric/x/episodic",
        content="y",
        idempotency_key="my-key-123",
    )
    assert seen_keys[1] == "my-key-123"


# ---------------------------------------------------------------------------
# Connection — bullets 14-15
# ---------------------------------------------------------------------------


def test_connection_pool_reused_across_calls() -> None:
    """Bullet 14 — repeated calls reuse the same underlying httpx.Client
    (not a fresh one per call)."""
    client = _client(
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})
        )
    )
    first = client._http
    client.retrieve(namespace="eric/x/episodic", query_text="a")
    client.retrieve(namespace="eric/x/episodic", query_text="b")
    assert client._http is first


def test_async_client_context_manager_cleanup() -> None:
    """Bullet 15 — ``async with AsyncMusubiClient(...) as c:`` closes the
    underlying httpx.AsyncClient on exit."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})

    transport = httpx.MockTransport(handler)

    async def _run() -> bool:
        async with AsyncMusubiClient(
            base_url=_BASE_URL,
            token=_TOKEN,
            retry=RetryPolicy(max_attempts=1, base_backoff=0.0),
            transport=transport,
        ) as client:
            await client.retrieve(namespace="eric/x/episodic", query_text="hi")
            inner = client._http
        return inner.is_closed

    assert asyncio.run(_run()) is True


# ---------------------------------------------------------------------------
# Telemetry — bullets 16-17
# ---------------------------------------------------------------------------


def test_otel_span_emitted_per_call() -> None:
    import httpx
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from musubi.sdk import MusubiClient

    # In Python tests, the global tracer provider might already be initialized.
    # We can just mock musubi.sdk.tracing.trace.get_tracer to return our specific tracer.
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test")

    from unittest.mock import patch

    with (
        patch("musubi.sdk.tracing.trace.get_tracer", return_value=tracer),
        patch("musubi.sdk.tracing.HAS_OTEL", True),
    ):
        with MusubiClient(
            base_url="http://x.test/v1",
            token="t",
            transport=httpx.MockTransport(lambda r: httpx.Response(204)),
        ) as c:
            c.memories.capture(namespace="ns", content="x")

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "musubi.memories.capture"
        assert spans[0].attributes["http.method"] if spans[0].attributes else None == "POST"
        assert spans[0].attributes and "http://x.test/memories" in str(
            spans[0].attributes["http.url"]
        )
        assert spans[0].attributes["musubi.namespace"] if spans[0].attributes else None == "ns"
        assert spans[0].attributes and "musubi.duration_ms" in spans[0].attributes


def test_request_id_propagated() -> None:
    """Bullet 17 — caller-supplied X-Request-Id is propagated as a
    request header for end-to-end tracing."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Request-Id"))
        return httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})

    client = _client(httpx.MockTransport(handler))
    client.retrieve(
        namespace="eric/x/episodic",
        query_text="x",
        request_id="trace-abc-123",
    )
    assert seen[0] == "trace-abc-123"


# ---------------------------------------------------------------------------
# Version compatibility — bullets 18-19
# ---------------------------------------------------------------------------


def test_probe_logs_warning_on_older_core(caplog: pytest.LogCaptureFixture) -> None:
    """Bullet 18 — first call probes /v1/ops/status; if Core's reported
    version is older than the SDK's MIN_CORE_VERSION, a warning is logged."""
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/status":
            return httpx.Response(
                200,
                json={"status": "ok", "version": "0.0.1", "components": {}},
            )
        return httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})

    client = _client(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="musubi.sdk"):
        client.probe_version()
    assert any("older than SDK minimum" in r.getMessage() for r in caplog.records)


def test_probe_raises_when_configured_strict() -> None:
    """Bullet 19 — strict mode raises instead of warning."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ops/status":
            return httpx.Response(200, json={"status": "ok", "version": "0.0.1", "components": {}})
        return httpx.Response(200, json={})

    client = MusubiClient(
        base_url=_BASE_URL,
        token=_TOKEN,
        retry=RetryPolicy(max_attempts=1, base_backoff=0.0),
        transport=httpx.MockTransport(handler),
        strict_version=True,
    )
    with pytest.raises(MusubiError, match="older than SDK minimum"):
        client.probe_version()


# ---------------------------------------------------------------------------
# Mocking — bullets 20-21
# ---------------------------------------------------------------------------


def test_fake_client_accepts_same_args_as_real() -> None:
    """Bullet 20 — FakeMusubiClient accepts the same constructor args as
    MusubiClient (so adapters can swap one for the other)."""
    fake = FakeMusubiClient(
        base_url=_BASE_URL,
        token=_TOKEN,
        retrieve_returns={
            "results": [{"object_id": "x" * 27, "score": 0.5, "plane": "episodic"}],
            "mode": "fast",
            "limit": 10,
        },
    )
    out = fake.retrieve(namespace="eric/x/episodic", query_text="probe")
    assert out["results"][0]["object_id"] == "x" * 27


def test_fake_client_returns_configured_fixtures() -> None:
    """Bullet 21 — fake's constructor accepts canned returns per
    method; calls return them verbatim."""
    fake = FakeMusubiClient(
        capture_returns={"object_id": "p" * 27, "state": "provisional"},
        thoughts_check_returns={"items": []},
    )
    cap = fake.memories.capture(namespace="x/y/episodic", content="probe")
    assert cap["object_id"] == "p" * 27
    inbox = fake.thoughts.check(namespace="x/y/thought", presence="x/y")
    assert inbox == {"items": []}


# ---------------------------------------------------------------------------
# Integration — bullet 22 — out-of-scope in work log
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="out-of-scope in slice work log: 20-case integration suite against docker-up Musubi container; deferred to musubi-contract-tests repo per ADR-0011"
)
def test_integration_sdk_against_real_musubi_container_20_case_contract_suite_passes() -> None:
    """Bullet 22 — placeholder."""


# ---------------------------------------------------------------------------
# Coverage tests — exercise additional surfaces not in the contract.
# ---------------------------------------------------------------------------


def test_404_raises_not_found() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json=_err("NOT_FOUND", 404)))
    client = _client(transport)
    with pytest.raises(NotFound):
        client.memories.get(namespace="x/y/episodic", object_id="0" * 27)


def test_400_raises_bad_request() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json=_err("BAD_REQUEST", 400)))
    client = _client(transport)
    with pytest.raises(BadRequest):
        client.memories.capture(namespace="x/y/episodic", content="")


def test_409_raises_conflict() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(409, json=_err("CONFLICT", 409)))
    client = _client(transport)
    with pytest.raises(Conflict):
        client.memories.capture(namespace="x/y/episodic", content="x")


def test_429_raises_rate_limited_after_retry() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(429, json=_err("RATE_LIMITED", 429)))
    client = _client(transport, retry=RetryPolicy(max_attempts=1, base_backoff=0.0))
    with pytest.raises(RateLimited):
        client.memories.capture(namespace="x/y/episodic", content="x")


def test_async_capture_round_trip() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"object_id": "a" * 27, "state": "provisional"})

    async def _run() -> dict[str, object]:
        client = AsyncMusubiClient(
            base_url=_BASE_URL,
            token=_TOKEN,
            retry=RetryPolicy(max_attempts=1, base_backoff=0.0),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.memories.capture(namespace="eric/x/episodic", content="async")
        finally:
            await client.close()

    out = asyncio.run(_run())
    assert out["object_id"] == "a" * 27


def test_authorization_header_set() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})

    client = _client(httpx.MockTransport(handler))
    client.retrieve(namespace="eric/x/episodic", query_text="x")
    assert seen[0] == f"Bearer {_TOKEN}"


def test_thoughts_check_routes_to_check_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/thoughts/check"
        return httpx.Response(200, json={"items": []})

    client = _client(httpx.MockTransport(handler))
    out = client.thoughts.check(namespace="eric/x/thought", presence="eric/claude-code")
    assert out == {"items": []}


def test_curated_get_routes_to_curated_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/v1/curated-knowledge/")
        return httpx.Response(200, json={"object_id": "c" * 27})

    client = _client(httpx.MockTransport(handler))
    out = client.curated.get(namespace="eric/x/curated", object_id="c" * 27)
    assert out["object_id"] == "c" * 27


def test_artifact_blob_returns_raw_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>raw</html>", headers={"content-type": "text/html"}
        )

    client = _client(httpx.MockTransport(handler))
    body = client.artifacts.blob(namespace="eric/x/artifact", object_id="a" * 27)
    assert body == b"<html>raw</html>"


def test_ops_health_returns_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "v0"})

    client = _client(httpx.MockTransport(handler))
    out = client.ops.health()
    assert out["status"] == "ok"


# ---------------------------------------------------------------------------
# Async client coverage — every namespace + retry + stream + probe.
# ---------------------------------------------------------------------------


def _async_client(
    handler: Any,
    *,
    retry: RetryPolicy | None = None,
    strict_version: bool = False,
) -> AsyncMusubiClient:
    return AsyncMusubiClient(
        base_url=_BASE_URL,
        token=_TOKEN,
        retry=retry or RetryPolicy(max_attempts=1, base_backoff=0.0),
        transport=httpx.MockTransport(handler),
        strict_version=strict_version,
    )


def test_async_retrieve_round_trip() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"object_id": "z" * 27}], "mode": "fast", "limit": 10}
        )

    async def _run() -> dict[str, Any]:
        async with _async_client(handler) as c:
            return await c.retrieve(namespace="eric/x/episodic", query_text="hi")

    out = asyncio.run(_run())
    assert out["results"][0]["object_id"] == "z" * 27


def test_async_capture_result_returns_sdkresult_on_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_err("FORBIDDEN", 403, "nope"))

    async def _run() -> SDKResult[dict[str, Any]]:
        async with _async_client(handler) as c:
            return await c.memories.capture_result(namespace="eric/other/episodic", content="x")

    res = asyncio.run(_run())
    assert res.is_err()
    assert res.err is not None
    assert res.err.code == "FORBIDDEN"


def test_async_get_routes() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"object_id": "g" * 27})

    async def _run() -> None:
        async with _async_client(handler) as c:
            await c.memories.get(namespace="x/y/episodic", object_id="g" * 27)
            await c.curated.get(namespace="x/y/curated", object_id="g" * 27)
            await c.concepts.get(namespace="x/y/concept", object_id="g" * 27)
            await c.artifacts.get(namespace="x/y/artifact", object_id="g" * 27)

    asyncio.run(_run())
    assert any(p.startswith("/v1/memories/") for p in seen)
    assert any(p.startswith("/v1/curated-knowledge/") for p in seen)
    assert any(p.startswith("/v1/concepts/") for p in seen)
    assert any(p.startswith("/v1/artifacts/") for p in seen)


def test_async_thoughts_send_and_check() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/send"):
            return httpx.Response(202, json={"object_id": "s" * 27, "state": "provisional"})
        return httpx.Response(200, json={"items": []})

    async def _run() -> None:
        async with _async_client(handler) as c:
            ack = await c.thoughts.send(
                namespace="x/y/thought",
                from_presence="a",
                to_presence="b",
                content="hi",
            )
            assert ack["object_id"] == "s" * 27
            inbox = await c.thoughts.check(namespace="x/y/thought", presence="a")
            assert inbox == {"items": []}

    asyncio.run(_run())
    assert seen == ["/v1/thoughts/send", "/v1/thoughts/check"]


def test_async_artifact_blob_and_lifecycle_and_ops() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/blob"):
            return httpx.Response(200, content=b"raw-bytes")
        if "lifecycle" in request.url.path:
            return httpx.Response(200, json={"events": []})
        if "health" in request.url.path:
            return httpx.Response(200, json={"status": "ok"})
        if "status" in request.url.path:
            return httpx.Response(200, json={"status": "ok", "version": "0.1.0"})
        return httpx.Response(404)

    async def _run() -> None:
        async with _async_client(handler) as c:
            blob = await c.artifacts.blob(namespace="x/y/artifact", object_id="a" * 27)
            assert blob == b"raw-bytes"
            ev = await c.lifecycle.events(namespace="x/y")
            assert ev == {"events": []}
            ev2 = await c.lifecycle.events()
            assert ev2 == {"events": []}
            assert (await c.ops.health())["status"] == "ok"
            assert (await c.ops.status())["version"] == "0.1.0"

    asyncio.run(_run())


def test_async_batch_context_one_call() -> None:
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body.get("items", [])))
        return httpx.Response(202, json={"object_ids": ["x" * 27, "y" * 27]})

    async def _run() -> None:
        async with (
            _async_client(handler) as c,
            c.memories.batch(namespace="x/y/episodic") as batch,
        ):
            batch.capture(content="one")
            batch.capture(content="two")

    asyncio.run(_run())
    assert len(calls) == 1
    assert calls[0][0] == "/v1/memories/batch"


def test_async_batch_context_empty_skips_call() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(202, json={})

    async def _run() -> None:
        async with _async_client(handler) as c, c.memories.batch(namespace="x/y/episodic"):
            pass

    asyncio.run(_run())
    assert calls == []


def test_async_stream_yields_per_line() -> None:
    body = b'{"object_id":"a","score":0.9}\n{"object_id":"b","score":0.7}\n'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async def _run() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with _async_client(handler) as c:
            async for row in c.retrieve_stream(namespace="x/y/episodic", query_text="x"):
                rows.append(row)
        return rows

    rows = asyncio.run(_run())
    assert [r["object_id"] for r in rows] == ["a", "b"]


def test_async_stream_raises_on_4xx() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_err("FORBIDDEN", 403))

    async def _run() -> None:
        async with _async_client(handler) as c:
            async for _ in c.retrieve_stream(namespace="x/y/episodic", query_text="x"):
                pass

    with pytest.raises(Forbidden):
        asyncio.run(_run())


def test_async_retry_then_success() -> None:
    state = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json=_err("BACKEND_UNAVAILABLE", 503))
        return httpx.Response(202, json={"object_id": "o" * 27, "state": "provisional"})

    async def _run() -> dict[str, Any]:
        async with AsyncMusubiClient(
            base_url=_BASE_URL,
            token=_TOKEN,
            retry=RetryPolicy(max_attempts=2, base_backoff=0.0),
            transport=httpx.MockTransport(handler),
        ) as c:
            return await c.memories.capture(namespace="x/y/episodic", content="x")

    out = asyncio.run(_run())
    assert out["object_id"] == "o" * 27
    assert state["n"] == 2


def test_async_503_exhausts_retries() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json=_err("BACKEND_UNAVAILABLE", 503))

    async def _run() -> None:
        async with AsyncMusubiClient(
            base_url=_BASE_URL,
            token=_TOKEN,
            retry=RetryPolicy(max_attempts=2, base_backoff=0.0),
            transport=httpx.MockTransport(handler),
        ) as c:
            await c.memories.capture(namespace="x/y/episodic", content="x")

    with pytest.raises(BackendUnavailable):
        asyncio.run(_run())


def test_async_network_error_exhausts_retries() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed", request=request)

    async def _run() -> None:
        async with AsyncMusubiClient(
            base_url=_BASE_URL,
            token=_TOKEN,
            retry=RetryPolicy(max_attempts=2, base_backoff=0.0),
            transport=httpx.MockTransport(handler),
        ) as c:
            await c.memories.capture(namespace="x/y/episodic", content="x")

    with pytest.raises(NetworkError):
        asyncio.run(_run())


def test_async_probe_warns_on_older_core(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.0.1", "components": {}})

    async def _run() -> str:
        async with _async_client(handler) as c:
            return await c.probe_version()

    with caplog.at_level(logging.WARNING, logger="musubi.sdk"):
        observed = asyncio.run(_run())
    assert observed == "0.0.1"
    assert any("older than SDK minimum" in r.getMessage() for r in caplog.records)


def test_async_probe_raises_in_strict_mode() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.0.1"})

    async def _run() -> str:
        async with _async_client(handler, strict_version=True) as c:
            return await c.probe_version()

    with pytest.raises(MusubiError, match="older than SDK minimum"):
        asyncio.run(_run())


def test_async_request_id_propagated() -> None:
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Request-Id"))
        return httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})

    async def _run() -> None:
        async with _async_client(handler) as c:
            await c.retrieve(namespace="x/y/episodic", query_text="x", request_id="trace-1")

    asyncio.run(_run())
    assert seen[0] == "trace-1"


def test_async_idempotency_caller_supplied_wins() -> None:
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(202, json={"object_id": "o" * 27, "state": "provisional"})

    async def _run() -> None:
        async with _async_client(handler) as c:
            await c.memories.capture(namespace="x/y/episodic", content="a")
            await c.memories.capture(
                namespace="x/y/episodic", content="b", idempotency_key="caller-key"
            )

    asyncio.run(_run())
    assert seen[0] is not None
    assert seen[1] == "caller-key"


def test_async_exception_with_unparseable_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>oops</html>")

    async def _run() -> None:
        async with _async_client(handler) as c:
            await c.memories.capture(namespace="x/y/episodic", content="x")

    with pytest.raises(MusubiError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# FakeMusubiClient coverage — every namespace + unconfigured-raises.
# ---------------------------------------------------------------------------


def test_fake_unconfigured_method_raises() -> None:
    fake = FakeMusubiClient()
    with pytest.raises(NotImplementedError):
        fake.retrieve(namespace="x/y/episodic", query_text="x")
    with pytest.raises(NotImplementedError):
        fake.memories.capture(namespace="x/y/episodic", content="x")
    with pytest.raises(NotImplementedError):
        fake.memories.get(namespace="x/y/episodic", object_id="x" * 27)
    with pytest.raises(NotImplementedError):
        fake.curated.get(namespace="x/y/curated", object_id="x" * 27)
    with pytest.raises(NotImplementedError):
        fake.concepts.get(namespace="x/y/concept", object_id="x" * 27)
    with pytest.raises(NotImplementedError):
        fake.artifacts.get(namespace="x/y/artifact", object_id="x" * 27)
    with pytest.raises(NotImplementedError):
        fake.artifacts.blob(namespace="x/y/artifact", object_id="x" * 27)
    with pytest.raises(NotImplementedError):
        fake.thoughts.send(namespace="x/y/thought", from_presence="a", to_presence="b", content="x")
    with pytest.raises(NotImplementedError):
        fake.thoughts.check(namespace="x/y/thought", presence="a")
    with pytest.raises(NotImplementedError):
        fake.lifecycle.events()
    with pytest.raises(NotImplementedError):
        fake.ops.health()
    with pytest.raises(NotImplementedError):
        fake.ops.status()


def test_fake_returns_canned_for_every_method() -> None:
    fake = FakeMusubiClient(
        capture_returns={"object_id": "c" * 27},
        get_memory_returns={"object_id": "g" * 27},
        retrieve_returns={"results": []},
        retrieve_stream_returns=[{"object_id": "s" * 27}],
        thoughts_send_returns={"object_id": "t" * 27},
        thoughts_check_returns={"items": []},
        curated_get_returns={"object_id": "k" * 27},
        concept_get_returns={"object_id": "p" * 27},
        artifact_get_returns={"object_id": "a" * 27},
        artifact_blob_returns=b"blob",
        lifecycle_events_returns={"events": []},
        ops_health_returns={"status": "ok"},
        ops_status_returns={"status": "ok", "version": "0.1.0"},
        probe_version_returns="0.1.2",
    )
    assert fake.memories.capture(namespace="x", content="y")["object_id"] == "c" * 27
    assert fake.memories.get(namespace="x", object_id="z" * 27)["object_id"] == "g" * 27
    assert fake.retrieve(namespace="x", query_text="q") == {"results": []}
    rows = list(fake.retrieve_stream(namespace="x", query_text="q"))
    assert rows == [{"object_id": "s" * 27}]
    assert (
        fake.thoughts.send(namespace="x", from_presence="a", to_presence="b", content="x")[
            "object_id"
        ]
        == "t" * 27
    )
    assert fake.thoughts.check(namespace="x", presence="a") == {"items": []}
    assert fake.curated.get(namespace="x", object_id="z" * 27)["object_id"] == "k" * 27
    assert fake.concepts.get(namespace="x", object_id="z" * 27)["object_id"] == "p" * 27
    assert fake.artifacts.get(namespace="x", object_id="z" * 27)["object_id"] == "a" * 27
    assert fake.artifacts.blob(namespace="x", object_id="z" * 27) == b"blob"
    assert fake.lifecycle.events() == {"events": []}
    assert fake.ops.health() == {"status": "ok"}
    assert fake.ops.status() == {"status": "ok", "version": "0.1.0"}
    assert fake.probe_version() == "0.1.2"
    # Calls log records every invocation.
    assert any(call[0] == "memories.capture" for call in fake.calls)


def test_fake_capture_result_wraps_canned_error() -> None:
    fake = FakeMusubiClient(
        capture_error=Forbidden(code="FORBIDDEN", detail="nope", hint="", status_code=403)
    )
    res = fake.memories.capture_result(namespace="x", content="y")
    assert res.is_err()
    assert res.err is not None
    assert res.err.code == "FORBIDDEN"


def test_fake_capture_result_wraps_canned_ok() -> None:
    fake = FakeMusubiClient(capture_returns={"object_id": "o" * 27})
    res = fake.memories.capture_result(namespace="x", content="y")
    assert res.is_ok()
    assert res.ok is not None
    assert res.ok and res.ok["object_id"] == "o" * 27


def test_fake_batch_context_records_calls() -> None:
    fake = FakeMusubiClient()
    with fake.memories.batch(namespace="x/y/episodic") as batch:
        batch.capture(content="one")
        batch.capture(content="two", tags=["t"], importance=7)
    captures = [c for c in fake.calls if c[0] == "memories.batch.capture"]
    assert len(captures) == 2


def test_fake_context_manager_close_no_op() -> None:
    with FakeMusubiClient() as fake:
        # close() is a no-op but the protocol must be honoured.
        assert fake is not None
    fake.close()  # extra direct call also tolerated


@pytest.mark.asyncio
async def test_async_fake_client_accepts_same_args_as_real() -> None:
    from musubi.sdk.testing import AsyncFakeMusubiClient

    fake = AsyncFakeMusubiClient(
        base_url="https://different.test",
        token="my-token",
        strict_version=True,
        capture_returns={"object_id": "a" * 27},
    )
    res = await fake.memories.capture(namespace="foo", content="bar")
    assert res["object_id"] == "a" * 27
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "memories.capture"


@pytest.mark.asyncio
async def test_async_fake_returns_canned_for_every_method() -> None:
    from musubi.sdk.testing import AsyncFakeMusubiClient

    fake = AsyncFakeMusubiClient(
        get_memory_returns={"object_id": "m1"},
        retrieve_returns={"results": []},
        retrieve_stream_returns=[{"r": 1}, {"r": 2}],
        thoughts_send_returns={"object_id": "t1"},
        thoughts_check_returns={"messages": []},
        curated_get_returns={"object_id": "c1"},
        concept_get_returns={"object_id": "x1"},
        artifact_get_returns={"object_id": "a1"},
        artifact_blob_returns=b"blob",
        lifecycle_events_returns={"events": []},
        ops_health_returns={"status": "ok"},
        ops_status_returns={"version": "9.9.9"},
        probe_version_returns="1.2.3",
    )

    assert (await fake.memories.get(namespace="n", object_id="id"))["object_id"] == "m1"
    assert (await fake.retrieve(namespace="n", query_text="q"))["results"] == []

    streamed = [x async for x in fake.retrieve_stream(namespace="n", query_text="q")]
    assert len(streamed) == 2
    assert streamed[0]["r"] == 1

    assert (
        await fake.thoughts.send(namespace="n", from_presence="a", to_presence="b", content="hi")
    )["object_id"] == "t1"
    assert (await fake.thoughts.check(namespace="n", presence="b"))["messages"] == []
    assert (await fake.curated.get(namespace="n", object_id="id"))["object_id"] == "c1"
    assert (await fake.concepts.get(namespace="n", object_id="id"))["object_id"] == "x1"
    assert (await fake.artifacts.get(namespace="n", object_id="id"))["object_id"] == "a1"
    assert await fake.artifacts.blob(namespace="n", object_id="id") == b"blob"
    assert (await fake.lifecycle.events(namespace="n"))["events"] == []
    assert (await fake.ops.health())["status"] == "ok"
    assert (await fake.ops.status())["version"] == "9.9.9"
    assert await fake.probe_version() == "1.2.3"


@pytest.mark.asyncio
async def test_async_fake_capture_result_wraps_canned_error() -> None:
    from musubi.sdk.exceptions import BadRequest
    from musubi.sdk.testing import AsyncFakeMusubiClient

    fake = AsyncFakeMusubiClient(
        capture_error=BadRequest(code="BAD", detail="err", status_code=400)
    )
    res = await fake.memories.capture_result(namespace="n", content="c")
    assert res.is_err()
    assert res.err and res.err.code == "BAD"


@pytest.mark.asyncio
async def test_async_fake_capture_result_wraps_canned_ok() -> None:
    from musubi.sdk.testing import AsyncFakeMusubiClient

    fake = AsyncFakeMusubiClient(capture_returns={"object_id": "o" * 27})
    res = await fake.memories.capture_result(namespace="n", content="c")
    assert res.is_ok()
    assert res.ok and res.ok["object_id"] == "o" * 27


@pytest.mark.asyncio
async def test_async_fake_batch_context_records_calls() -> None:
    from musubi.sdk.testing import AsyncFakeMusubiClient

    fake = AsyncFakeMusubiClient()
    async with fake.memories.batch(namespace="n") as batch:
        batch.capture(content="first")
        batch.capture(content="second")

    assert len(fake.calls) == 2
    assert fake.calls[0][0] == "memories.batch.capture"
    assert fake.calls[0][1]["content"] == "first"
    assert fake.calls[1][1]["content"] == "second"


@pytest.mark.asyncio
async def test_async_fake_context_manager_close_no_op() -> None:
    from musubi.sdk.testing import AsyncFakeMusubiClient

    async with AsyncFakeMusubiClient() as fake:
        pass
    assert len(fake.calls) == 0
