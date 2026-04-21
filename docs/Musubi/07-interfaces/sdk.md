---
title: Python SDK
section: 07-interfaces
tags: [interfaces, python, sdk, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-19
up: "[[07-interfaces/index]]"
reviewed: false
implements: ["src/musubi/sdk/", "tests/sdk/"]
---
# Python SDK

`musubi.sdk` — the Python package every adapter uses. A thin, typed, typed-error-returning wrapper around the canonical API.

The SDK is ours. Adapters (MCP, LiveKit, OpenClaw) consume it. End-user programs can use it too.

> **Layout note (ADR-0015 / ADR-0016):** the SDK ships as a sub-package
> of the monorepo (`src/musubi/sdk/`), not the pre-monorepo sibling
> `musubi-client/` package. Imports are `from musubi.sdk import …`.

## Package

```
src/musubi/sdk/
  __init__.py            # re-exports MusubiClient, AsyncMusubiClient, exceptions, RetryPolicy, SDKResult
  client.py              # MusubiClient class
  async_client.py        # AsyncMusubiClient class
  exceptions.py          # typed errors
  result.py              # SDKResult[T] wrapper
  retry.py               # retry policy
  testing.py             # FakeMusubiClient
```

Import path: `musubi.sdk` (sub-package of `musubi`). Version tracks the
monorepo's release; SDK-only changes still bump that release.

## Public surface

### Construction

```python
from musubi.sdk import MusubiClient

client = MusubiClient(
    base_url="https://musubi.example.local.example.com/v1",
    token="eyJhbGc...",
    timeout=30,                         # seconds, overall; per-call timeouts override
    retry=RetryPolicy.default(),
)
```

Async variant:

```python
from musubi.sdk import AsyncMusubiClient

async with AsyncMusubiClient(...) as client:
    res = await client.retrieve(...)
```

The async variant uses `httpx.AsyncClient`; sync uses `httpx.Client`. Shared `models.py` between them.

### Common methods

```python
# Capture
memory = client.memories.capture(
    namespace="eric/claude-code/episodic",
    content="...",
    tags=["cuda"],
    topics=["infrastructure/gpu"],
    importance=7,
)

# Retrieve
results = client.retrieve(
    RetrievalQuery(
        namespace="eric/_shared/blended",
        query_text="...",
        mode="fast",
        limit=5,
    )
)

# Thoughts
client.thoughts.send(
    from_presence="claude-code",
    to_presence="livekit-voice",
    content="...",
)

unread = client.thoughts.check(my_presence="livekit-voice")
client.thoughts.read(my_presence="livekit-voice", ids=[t.object_id for t in unread])
```

### Resource modules

- `client.memories` — capture, get, batch, archive
- `client.curated` — get, patch-metadata, list
- `client.concepts` — get, reinforce, promote, reject, list
- `client.artifacts` — upload, get, blob, chunks, archive
- `client.thoughts` — send, check, read, history
- `client.lifecycle` — events, transition (operator), reconcile (operator)
- `client.ops` — health, status

Each resource is a typed namespace on the client; methods mirror the canonical endpoints.

## Errors

```python
from musubi.sdk.exceptions import (
    MusubiError,           # base
    BadRequest,            # 400
    Unauthorized,          # 401
    Forbidden,             # 403
    NotFound,              # 404
    Conflict,              # 409
    RateLimited,           # 429
    BackendUnavailable,    # 503
    InternalError,         # 500
    NetworkError,          # lower-level (DNS, connect)
)

try:
    client.memories.capture(...)
except Forbidden as e:
    logger.warning("namespace %s out of scope", e.detail.namespace)
except BackendUnavailable:
    # retry is handled inside SDK; surface only if retries exhausted
    pass
```

Errors carry structured `detail` fields matching the API's error schema. No bare strings.

## Result[T, E] pattern

The SDK also offers a Result-oriented surface for adapters that prefer typed errors over exceptions:

```python
res = client.memories.capture_result(...)
if res.is_err():
    log.warning("capture failed: %s", res.err.code)
    return
memory = res.ok
```

Both styles wrap the same underlying HTTP call. Pick per adapter preference.

## Retry policy

Default retry policy:

- Retries on: 429, 503, 504, `NetworkError`.
- Exponential backoff: 0.5s, 1s, 2s, 4s (max 4 attempts).
- Honors `Retry-After` header on 429/503.
- Idempotency key auto-generated for POST operations unless the caller provides one.

Override:

```python
client = MusubiClient(retry=RetryPolicy(max_attempts=6, base_backoff=0.3))
```

## Connection pooling

One `httpx.Client` per SDK instance. Connection pool sized to max concurrency on the adapter's workload. Default pool: 20 connections / host. Tunable.

## Telemetry hooks

The SDK emits OpenTelemetry spans (when OTel is configured in the adapter) for each call. Span name matches the method (`musubi.memories.capture`). Default attributes: `http.method`, `http.url`, `musubi.namespace`, `musubi.duration_ms`.

If the adapter sets `X-Request-Id` in the caller's context, the SDK propagates it as a header for end-to-end tracing.

## Batch helpers

Pack multiple operations that the API supports in batch form:

```python
with client.memories.batch() as batch:
    batch.capture(...)
    batch.capture(...)
    batch.capture(...)
# on exit, one POST /v1/memories/batch call; results attached to local references
```

## Streaming retrieval

```python
for result in client.retrieve_stream(RetrievalQuery(limit=500, ...)):
    handle(result)
```

Uses `POST /v1/retrieve/stream` (NDJSON). Generator yields `RetrievalResult` objects one at a time.

## Version compatibility

The SDK pins a minimum Musubi Core version it supports. On first use, it probes `GET /v1/ops/status` and logs a warning if the Core version is below the minimum. Adapters can configure this to be a hard error.

## Mocking for tests

Adapters' unit tests mock the SDK:

```python
from musubi.sdk.testing import FakeMusubiClient

fake = FakeMusubiClient(
    retrieve_returns=[
        RetrievalResult(...),
    ],
    thoughts_check_returns=[],
)
adapter = MyAdapter(client=fake)
...
```

`FakeMusubiClient` matches the real client's signature + return types, using pydantic models for fixtures.

Integration tests against a real Musubi instance use a shared test-container fixture (see [[07-interfaces/contract-tests]]).

## Packaging

- Python 3.12+.
- Runtime deps: `httpx`, `pydantic>=2.0`, `orjson`.
- Optional extras: `grpcio` for gRPC support (re-exported from the parent monorepo's `[grpc]` extra).
- Test deps (dev extra): `pytest`, `pytest-asyncio`, `respx`.

## Test Contract

**Module under test:** `src/musubi/sdk/*.py`

Happy path:

1. `test_capture_returns_memory_model`
2. `test_retrieve_returns_list_of_results`
3. `test_thoughts_send_returns_acknowledgement`
4. `test_batch_context_one_http_call`
5. `test_stream_yields_per_ndjson_line`

Errors:

6. `test_401_raises_unauthorized`
7. `test_403_raises_forbidden_with_detail`
8. `test_503_retries_then_raises_backend_unavailable`
9. `test_network_error_retried`
10. `test_result_api_mirrors_exception_api`

Retry:

11. `test_retry_honors_retry_after_header`
12. `test_retry_exponential_backoff_respects_max_attempts`
13. `test_idempotency_key_auto_generated_on_post`

Connection:

14. `test_connection_pool_reused_across_calls`
15. `test_async_client_context_manager_cleanup`

Telemetry:

16. `test_otel_span_emitted_per_call`
17. `test_request_id_propagated`

Version compatibility:

18. `test_probe_logs_warning_on_older_core`
19. `test_probe_raises_when_configured_strict`

Mocking:

20. `test_fake_client_accepts_same_args_as_real`
21. `test_fake_client_returns_configured_fixtures`

Integration:

22. `integration: SDK against a real Musubi container — 20-case contract suite passes`
