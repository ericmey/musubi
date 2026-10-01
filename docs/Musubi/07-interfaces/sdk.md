---
title: Python SDK
section: 07-interfaces
tags: [interfaces, python, sdk, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
implements: ["src/musubi/sdk/", "tests/sdk/"]
---
# Python SDK

There are two Python clients for the canonical API. Pick by where your code lives.

| Package | Where | For |
|---|---|---|
| **`musubi-sdk`** (`import musubi_sdk`) | separate repository [`sourceblender/musubi-sdk`](https://github.com/sourceblender/musubi-sdk), published to PyPI | Your own programs and integrations. Depends only on `httpx`. See [Connect](../../guide/connect.md) and [Use it](../../guide/use.md). |
| **`musubi.sdk`** | this repository, `src/musubi/sdk/` | Code inside Core, mainly the in-repo MCP adapter ([[07-interfaces/mcp-adapter]]) and tests. Not published separately. |

The rest of this page documents the **in-repo** `musubi.sdk`. For the external package, its own repository is the reference.

Both are thin wrappers over the HTTP API in [[07-interfaces/canonical-api]]. Where this page and `openapi.yaml` disagree, `openapi.yaml` wins.

## Package

```
src/musubi/sdk/
  __init__.py       # re-exports MusubiClient, AsyncMusubiClient, exceptions, RetryPolicy, SDKResult
  client.py         # MusubiClient (httpx.Client)
  async_client.py   # AsyncMusubiClient (httpx.AsyncClient), same surface
  exceptions.py     # typed errors
  result.py         # SDKResult[T]
  retry.py          # RetryPolicy
  tracing.py        # optional OpenTelemetry spans
  testing.py        # FakeMusubiClient, AsyncFakeMusubiClient
```

Import path: `from musubi.sdk import ...`. It is versioned with the Core release. Responses are returned as plain `dict`s (parsed JSON); there are no response model classes.

## Construction

```python
from musubi.sdk import MusubiClient, RetryPolicy

client = MusubiClient(
    base_url="http://127.0.0.1:8100/v1",   # include /v1
    token="eyJhbGc...",
    timeout=30.0,
    retry=RetryPolicy.default(),
    strict_version=False,
)
```

Async variant:

```python
from musubi.sdk import AsyncMusubiClient

async with AsyncMusubiClient(base_url=..., token=...) as client:
    res = await client.retrieve(namespace="alex/claude-code", query_text="...", planes=["episodic"])
```

## Methods

The full surface (sync shown; async is the same with `await`):

```python
# Top level
client.retrieve(namespace=None, query_text="", mode="fast", limit=10,
                planes=None, since=None, tags=None, request_id=None) -> dict
client.retrieve_stream(namespace=None, query_text=..., mode="fast", limit=10,
                       request_id=None) -> Iterator[dict]      # NDJSON rows
client.probe_version() -> str
client.close()                                                # or use as a context manager

# Episodic
client.episodic.capture(namespace=..., content=..., tags=None, topics=None,
                        importance=5, idempotency_key=None, created_at=None) -> dict
client.episodic.capture_result(**kw) -> SDKResult[dict]
client.episodic.get(namespace=..., object_id=...) -> dict
client.episodic.batch(namespace=...)                          # context manager, see below

# Other planes (read only)
client.curated.get(namespace=..., object_id=...) -> dict
client.concepts.get(namespace=..., object_id=...) -> dict
client.artifacts.get(namespace=..., object_id=...) -> dict
client.artifacts.blob(namespace=..., object_id=...) -> bytes

# Thoughts
client.thoughts.send(namespace=..., from_presence=..., to_presence=..., content=...,
                     channel="default", importance=5) -> dict
client.thoughts.check(namespace=..., presence=...) -> dict

# Lifecycle and ops
client.lifecycle.events(namespace=None) -> dict               # needs an operator token
client.ops.health() -> dict
client.ops.status() -> dict
```

Notes:

- `thoughts.send` **requires** `namespace` (the sender's 3-segment thought namespace).
- `episodic.capture` sends a `topics` field, which the server ignores. Topics are inferred during maturation.
- `created_at` must be timezone-aware and needs an operator-scoped token.
- `retrieve` does not expose `state_filter`, `include_archived` or `include_lineage`; call the HTTP API directly for those.
- Not covered by this SDK: thought read/history/stream, curated/concept/artifact writes and lists, uploads, lifecycle transitions, context packs and idempotency receipts. Use HTTP for these, or the external `musubi-sdk`.

Example:

```python
client.episodic.capture(
    namespace="alex/claude-code/episodic",
    content="Switched the build to uv; lockfile committed.",
    tags=["build"],
    importance=6,
)

results = client.retrieve(
    namespace="alex/claude-code",
    query_text="how is the build managed",
    planes=["curated", "concept", "episodic"],
    limit=5,
)
for row in results["results"]:
    print(row["plane"], row["score"], row["content"][:80])

client.thoughts.send(
    namespace="alex/claude-code/thought",
    from_presence="claude-code",
    to_presence="voice",
    content="Build switched to uv.",
)
```

## Errors

```python
from musubi.sdk import (
    MusubiError,           # base: .code, .detail
    BadRequest,            # 400 (a 422 maps to the base MusubiError)
    Unauthorized,          # 401
    Forbidden,             # 403
    NotFound,              # 404
    Conflict,              # 409
    RateLimited,           # 429
    BackendUnavailable,    # 503
    InternalError,         # 500
    NetworkError,          # transport failure after retries
)

try:
    client.episodic.capture(...)
except Forbidden as e:
    log.warning("out of scope: %s", e.detail)
except BackendUnavailable:
    pass  # retries already exhausted
```

Errors carry the API's `code` and `detail` strings.

## Result pattern

For callers that prefer values to exceptions:

```python
res = client.episodic.capture_result(namespace=..., content=...)
if res.is_err():
    log.warning("capture failed: %s", res.err.code)
else:
    memory = res.ok
```

`capture_result` is the only `*_result` method.

## Retry policy

`RetryPolicy.default()`:

- Retries on HTTP 429, 503, 504 and on transport errors.
- Exponential backoff: 0.5 s, 1 s, 2 s, capped at 4 s; at most **4 attempts** in total.
- Honours `Retry-After`, capped at 30 s.
- `RetryPolicy.none()` disables retries.

Every POST gets an auto-generated `Idempotency-Key` unless the caller passes one, so retried writes are replay-safe for 24 hours. `retrieve_stream` bypasses the retry loop.

## Connection pooling

One `httpx.Client` (or `AsyncClient`) per SDK instance, with httpx's default pool limits. Reuse one client per process.

## Request ids and tracing

Pass `request_id=` to `retrieve` / `retrieve_stream` to send `X-Request-Id`; Core echoes it and logs it.

With `opentelemetry-api` installed (the `otel` extra), every call runs inside a span named `musubi.<operation>` (for example `musubi.episodic.capture`) with attributes `http.method`, `http.url` (credentials scrubbed), `musubi.namespace`, `musubi.request_id` and `musubi.duration_ms`. Without OpenTelemetry the helpers are no-ops.

## Batch capture

```python
with client.episodic.batch(namespace="alex/claude-code/episodic") as batch:
    batch.capture(content="...")
    batch.capture(content="...", importance=7)
# on exit: one POST /v1/episodic/batch
```

An empty batch makes no call.

## Streaming retrieval

```python
for row in client.retrieve_stream(namespace="alex/claude-code/episodic", query_text="...", limit=200):
    handle(row)
```

Uses `POST /v1/retrieve/stream` and yields one dict per NDJSON line.

## Version check

`probe_version()` reads `GET /v1/ops/status` and compares the reported version with the SDK's minimum (`0.1.0`). It logs a warning when Core is older, or raises when the client was built with `strict_version=True`. It is not called automatically.

## Mocking for tests

```python
from musubi.sdk.testing import FakeMusubiClient

fake = FakeMusubiClient(
    retrieve_returns={"mode": "fast", "limit": 5, "warnings": [], "results": []},
    thoughts_check_returns={"items": []},
)
adapter = MyAdapter(client=fake)
...
assert fake.calls[0][0] == "retrieve"
```

`FakeMusubiClient` (and `AsyncFakeMusubiClient`) accept the real client's constructor arguments plus one canned return per method (`capture_returns`, `retrieve_returns`, `thoughts_send_returns`, and so on). An unconfigured method raises `NotImplementedError`. Every call is recorded in `.calls`.

## Packaging

- Python 3.12+.
- Runtime dependency: `httpx`, which ships with the `musubi` package.
- Optional: `opentelemetry-api` via the `otel` extra.
- There is no gRPC extra.

## Test Contract

**Module under test:** `src/musubi/sdk/*.py`

Happy path (`tests/sdk/test_sdk.py`):

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

Telemetry (`tests/sdk/test_tracing.py` as well):

16. `test_otel_span_emitted_per_call`
17. `test_request_id_propagated`

Version compatibility:

18. `test_probe_logs_warning_on_older_core`
19. `test_probe_raises_when_configured_strict`

Mocking:

20. `test_fake_client_accepts_same_args_as_real`
21. `test_fake_client_returns_configured_fixtures`

Warnings (`tests/sdk/test_ret007_sdk_warnings.py`):

22. degraded-retrieval `warnings` pass through unchanged

Integration (skipped; needs a running stack):

23. `integration: SDK against a real Musubi container — 20-case contract suite passes`
