---
title: "Add OpenTelemetry span emission to the Python SDK"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-sdk-py
target_slice: slice-sdk-py-otel-spans
status: resolved
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

# Add OpenTelemetry span emission to the Python SDK

## Source slice

`slice-sdk-py` (PR #90).

## Problem

`docs/architecture/07-interfaces/sdk.md` § Telemetry hooks specifies:

> The SDK emits OpenTelemetry spans (when OTel is configured in the
> adapter) for each call. Span name matches the method
> (`musubi.memories.capture`). Default attributes: `http.method`,
> `http.url`, `musubi.namespace`, `musubi.duration_ms`.

The SDK shipped without OTel emission because:

1. `opentelemetry-api` is not in the project's `[dev]` extras (would
   add ~12 transitive deps for every SDK install — including adapters
   that never enable OTel).
2. The spec says OTel is opt-in ("when OTel is configured in the
   adapter"), so a no-op default is consistent with the contract.
3. Test Contract bullet 16 (`test_otel_span_emitted_per_call`) is
   `@pytest.mark.skip` with this ticket cited.

## Requested change

Add OTel emission as an opt-in code path, gated on
`opentelemetry-api` being importable. Two implementation options:

**Option A — soft import in client.**

```python
try:
    from opentelemetry import trace
    _tracer = trace.get_tracer("musubi.sdk")
except ImportError:
    _tracer = None  # silently no-op
```

`_request()` wraps every call in `_tracer.start_as_current_span(...)`
when `_tracer is not None`. Adapter installs OTel; SDK picks it up.

**Option B — explicit `instrumentation` extras.**

`pyproject.toml`:

```toml
[project.optional-dependencies]
otel = ["opentelemetry-api>=1.27"]
```

Adapter installs `pip install -e ".[otel]"`. Same import-guard
pattern but with a clear extras flag.

Either works; B is preferred (explicit > implicit) but adds an extra
to track in the install matrix.

## Acceptance

- The SDK emits one span per HTTP call when OTel is configured;
  span name `musubi.<resource>.<method>` (e.g. `musubi.memories.capture`).
- Default attributes: `http.method`, `http.url` (with bearer scrubbed),
  `musubi.namespace`, `musubi.duration_ms`.
- `X-Request-Id` propagation already lives in the SDK; OTel span
  carries the same id as a `musubi.request_id` attribute.
- `test_otel_span_emitted_per_call` is unskipped and asserts on a
  spy tracer's recorded spans.
- The SDK has zero behaviour change when OTel is NOT configured —
  no import errors, no perf regression, no spans in process memory.

## Resolution

Resolved by PR #131.

## Resolution

Resolved by PR #131.
