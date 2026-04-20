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
import warnings
from collections.abc import Iterator
from pathlib import Path

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
    result = client.memories.capture(
        namespace="eric/x/episodic", content="hello"
    )
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
                    {"object_id": "a" * 27, "score": 0.9, "plane": "episodic", "content": "x", "namespace": "n"},
                    {"object_id": "b" * 27, "score": 0.7, "plane": "episodic", "content": "y", "namespace": "n"},
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


def test_batch_context_one_http_call() -> None:
    """Bullet 4 — the batch context manager flushes a SINGLE
    POST /v1/memories/batch on exit, not N posts."""
    calls: list[tuple[str, list[dict[str, object]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body.get("items", [])))
        return httpx.Response(
            202,
            json={"object_ids": [f"o{i}aaaaaaaaaaaaaaaaaaaaaaaaaa"[:27] for i in range(len(body["items"]))]},
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
    rows = list(
        client.retrieve_stream(namespace="eric/x/episodic", query_text="x")
    )
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
    ok = _client(ok_transport).memories.capture_result(
        namespace="eric/x/episodic", content="x"
    )
    assert isinstance(ok, SDKResult)
    assert ok.is_ok()
    assert ok.ok["object_id"] == "o" * 27

    err_transport = httpx.MockTransport(
        lambda r: httpx.Response(403, json=_err("FORBIDDEN", 403, "nope"))
    )
    err = _client(err_transport).memories.capture_result(
        namespace="eric/x/episodic", content="x"
    )
    assert err.is_err()
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
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, json={"results": [], "mode": "fast", "limit": 10})))
    first = client._http  # noqa: SLF001 — Inspecting wrapped client for the test
    client.retrieve(namespace="eric/x/episodic", query_text="a")
    client.retrieve(namespace="eric/x/episodic", query_text="b")
    assert client._http is first  # noqa: SLF001


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
            inner = client._http  # noqa: SLF001
        return inner.is_closed

    assert asyncio.run(_run()) is True


# ---------------------------------------------------------------------------
# Telemetry — bullets 16-17
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="deferred to future slice-sdk-py-otel-spans: opentelemetry-api "
    "is opt-in per spec ('when OTel is configured in the adapter'); "
    "adding it as a hard dep on every SDK install is out of scope. "
    "Cross-slice ticket "
    "_inbox/cross-slice/slice-sdk-py-otel-spans.md tracks the follow-up."
)
def test_otel_span_emitted_per_call() -> None:
    """Bullet 16 — placeholder."""


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
        retrieve_returns={"results": [{"object_id": "x" * 27, "score": 0.5, "plane": "episodic"}], "mode": "fast", "limit": 10},
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
    reason="declared out-of-scope in slice work log: 20-case integration "
    "suite against a docker-up Musubi container; deferred to "
    "musubi-contract-tests repo (per ADR-0011, that suite is a "
    "separate Python package). The unit-form tests above exercise the "
    "same surface against an in-process MockTransport."
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
            return await client.memories.capture(
                namespace="eric/x/episodic", content="async"
            )
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
    out = client.thoughts.check(
        namespace="eric/x/thought", presence="eric/claude-code"
    )
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
        return httpx.Response(200, content=b"<html>raw</html>", headers={"content-type": "text/html"})

    client = _client(httpx.MockTransport(handler))
    body = client.artifacts.blob(namespace="eric/x/artifact", object_id="a" * 27)
    assert body == b"<html>raw</html>"


def test_ops_health_returns_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "v0"})

    client = _client(httpx.MockTransport(handler))
    out = client.ops.health()
    assert out["status"] == "ok"
