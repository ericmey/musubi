---
title: OpenClaw Adapter
section: 07-interfaces
tags: [adapter, interfaces, openclaw, plugin, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
implements: "github.com/sourceblender/musubi-openclaw"
---
# OpenClaw Adapter

The OpenClaw integration lives in its own repository, [`sourceblender/musubi-openclaw`](https://github.com/sourceblender/musubi-openclaw), published to npm as `openclaw-musubi`. That repository is the reference for its features (capture mirror, prompt supplement, the canonical agent tools, thoughts over SSE), its configuration, its offline behaviour and its tests.

## What core contains

Nothing OpenClaw-specific. The plugin is an ordinary client of the HTTP API:

- **Capture:** `POST /v1/episodic` into the agent's presence namespace (`<agent>/<presence>/episodic`).
- **Recall:** `POST /v1/retrieve` with a 2-segment namespace and `planes`, or a wildcard namespace for cross-presence reads.
- **Thoughts:** `POST /v1/thoughts/send`, and `GET /v1/thoughts/stream` for push delivery. The stream's consumer expectations (backoff, persisted `Last-Event-ID`, dedup, no reconnect on 403) are in [[07-interfaces/canonical-api#Thoughts stream (SSE)]].

Token scopes follow the per-presence recommendation in [[07-interfaces/canonical-api#Recommended scope set for a per-presence token]].

## Offline behavior

What happens to captures while Musubi is unreachable (local queueing, retry, replay) is implemented and documented in the plugin repository, not in core. Core's side of the contract is idempotent writes: retried captures with the same `Idempotency-Key` are replay-safe for 24 hours (see [[07-interfaces/canonical-api#Idempotency]]).

## Related

- [Connect](../../guide/connect.md): the list of integrations and where each one lives.
- [[07-interfaces/agent-tools]]: the canonical tool surface integrations implement.

## Test Contract

Core has no OpenClaw-specific code or tests. The plugin's tests live in `musubi-openclaw`; the API behaviour it depends on is covered by `tests/api/` (capture, retrieve, `tests/api/test_thoughts_stream.py`).
