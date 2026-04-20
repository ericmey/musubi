---
title: "Add `AsyncFakeMusubiClient` to `musubi.sdk.testing`"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-adapter-livekit
target_slice: slice-sdk-py
status: resolved
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

# Add `AsyncFakeMusubiClient` to `musubi.sdk.testing`

## Source slice

`slice-adapter-livekit` (PR #96).

## Problem

The shipped `FakeMusubiClient` mirrors the **sync** `MusubiClient`
surface — every method (`memories.capture`, `thoughts.send`,
`retrieve`, …) is a regular function, not a coroutine.

Adapters that target `AsyncMusubiClient` (the LiveKit voice adapter,
likely the MCP adapter, eventually the OpenClaw adapter) need a fake
whose method signatures match the **async** client so call sites like
`await client.memories.capture(...)` work in unit tests.

The LiveKit adapter currently works around this by wrapping
`FakeMusubiClient` in a tiny `_AsyncFake` shim defined inside
`tests/adapters/test_livekit.py`. That shim re-implements the
namespace-attribute layout and re-exposes the `calls` log; every
adapter that lands afterwards will repeat the same boilerplate.

## Requested change

Add a sibling class to `src/musubi/sdk/testing.py`:

```python
class AsyncFakeMusubiClient:
    """Async drop-in fake mirroring AsyncMusubiClient's public surface.

    Same constructor signature + canned-return kwargs as
    FakeMusubiClient; every method that's async on AsyncMusubiClient
    is async here too. Shares the calls log shape so adapter tests
    can assert against (method_name, kwargs) tuples identically."""
```

Implementation sketch: the simplest path is a thin async wrapper that
holds a `FakeMusubiClient` internally and exposes coroutine versions
of each namespace method. The `_AsyncFake` / `_AsyncNamespace` shape
in `tests/adapters/test_livekit.py:49-91` is a reasonable starting
point — promote it into `musubi.sdk.testing` and the LiveKit /
MCP / OpenClaw adapters can drop their local copies.

## Acceptance

- `from musubi.sdk.testing import AsyncFakeMusubiClient` works.
- The LiveKit adapter's local `_AsyncFake` is deleted; tests still pass.
- The MCP + OpenClaw adapter slices can use the shared fake from day one.
- Coverage on `src/musubi/sdk/testing.py` does not regress.

## Resolution

Resolved by PR #129.
