---
title: Interfaces
section: 07-interfaces
tags: [adapters, api, interfaces, sdk, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# 07 — Interfaces

How the world reaches Musubi. One canonical API at the core; one SDK that speaks it; several adapters (independent projects) that translate between that SDK and specific surfaces (MCP, LiveKit, OpenClaw, HTTP, gRPC).

## Documents in this section

- [[07-interfaces/canonical-api]] — The authoritative interface contract. All surfaces derive from this.
- [[07-interfaces/sdk]] — The Python SDK. Adapters consume it; Musubi ships it.
- [[07-interfaces/mcp-adapter]] — Maps Musubi SDK to the Model Context Protocol (OAuth 2.1 finalized June 2025). Independent repo.
- [[07-interfaces/livekit-adapter]] — Maps Musubi SDK to LiveKit voice agents (Slow Thinker / Fast Talker). Independent repo.
- [[07-interfaces/openclaw-adapter]] — Maps Musubi SDK to the OpenClaw browser-extension agent. Independent repo.
- [[07-interfaces/contract-tests]] — The shared contract test suite every adapter passes.

## The ring

```
                     ┌────────────────────────────┐
                     │       Canonical API        │
                     │     (HTTP + gRPC + SDK)    │
                     └──────────────┬─────────────┘
                                    │
                ┌───────────────────┼───────────────────┐
                ▼                   ▼                   ▼
      ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
      │   MCP Adapter    │  │ LiveKit Adapter  │  │ OpenClaw Adapter │
      │  (independent    │  │  (independent    │  │  (independent    │
      │      repo)       │  │       repo)      │  │       repo)      │
      └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
               │                     │                     │
               ▼                     ▼                     ▼
            MCP tools           voice sessions         browser surface
         (coding agents)      (Slow Thinker/          (OpenClaw extension)
                              Fast Talker)
```

Adapters are **independent projects** — separate repos, separate deploy schedules, separate maintainers (even if the same person works on multiple). The Musubi core team owns the canonical API and the SDK; adapter teams own the mapping between their surface and the SDK.

## Why this separation

See [[03-system-design/abstraction-boundary]]. Summary:

- **Musubi Core** should not know what MCP is. An MCP adapter maps an MCP tool invocation to an SDK call and back.
- **LiveKit** should not know the details of vault sync or concept promotion. It consumes retrieval via the SDK.
- **Adapters evolve faster than Core.** MCP spec iterations, LiveKit SDK changes, browser extension quirks — those should not ripple into Musubi.

## Contract test pattern

Every adapter's CI runs the [[07-interfaces/contract-tests]] suite against a local Musubi instance:

- Capture an episodic memory → appears in retrieval.
- Send a thought → appears in `thought_check`.
- Concurrent writes → no lost updates.
- Forbidden namespace → 403.
- (…50+ canonical cases…)

If the Musubi version the adapter is built against doesn't pass the contract tests, the adapter build fails.

## Versioning

- **Canonical API**: SemVer per endpoint group. Breaking changes require a new path prefix (`/v2/…`) and a deprecation period of 180 days on `/v1/…`.
- **SDK**: SemVer tied to API version.
- **Adapters**: each adapter has its own SemVer; adapters pin a minimum Musubi version they support.

## Surfaces supported in v1

1. **HTTP / REST** — the canonical wire format. Every Musubi Core ships with it.
2. **gRPC** — same contract as HTTP, generated from `.proto`. Optional (behind a build flag); default-off in v1 for small-deployment simplicity.
3. **Python SDK** — `musubi-client` package, wraps HTTP.
4. **MCP Adapter** — uses the SDK. Runs as a separate service.
5. **LiveKit Adapter** — uses the SDK. Embedded in the voice agent worker.
6. **OpenClaw Adapter** — uses the SDK. Embedded in the extension's background service worker.

CLI (`musubi-cli`) is separate — an ops tool, not an end-user-facing adapter. Lives in the Musubi Core repo.

## Principles

1. **API is the only contract.** No adapter uses internal functions directly. Every adapter-test runs against the API.
2. **No surface-specific logic in Core.** Core doesn't know about MCP, LiveKit, or OpenClaw.
3. **Structured errors.** Typed errors with codes; human-readable messages; safe to show users.
4. **Idempotency by default.** Optional `Idempotency-Key` header supported everywhere that writes.
5. **Backward-compat first.** We keep old versions around long enough for adapters to catch up.
