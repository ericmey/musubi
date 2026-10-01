---
title: "07 — Interfaces"
section: 07-interfaces
tags: [adapters, api, interfaces, sdk, section/interfaces, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# 07 — Interfaces

How the world reaches Musubi: one canonical HTTP API in Core, thin clients that speak it, and integrations that translate between those clients and specific surfaces (MCP, voice, agent hosts).

## Documents in this section

- [[07-interfaces/canonical-api]]: the authoritative interface contract (with the root `openapi.yaml`). Everything else derives from it.
- [[07-interfaces/sdk]]: the Python clients. The public `musubi-sdk` lives in its own repository; the in-repo `musubi.sdk` backs the MCP adapter.
- [[07-interfaces/agent-tools]]: the canonical five-tool surface (`musubi_recent`, `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_think`). Backed by [[13-decisions/0032-agent-tools-canonical-surface]].
- [[07-interfaces/mcp-adapter]]: the in-repo MCP server (`src/musubi/adapters/mcp/`).
- [[07-interfaces/livekit-adapter]]: pointer to `musubi-livekit`; core keeps only a compatibility shim.
- [[07-interfaces/openclaw-adapter]]: pointer to `musubi-openclaw`.
- [[07-interfaces/contract-tests]]: what covers the API contract today; a shared cross-integration suite is future work.
- [[07-interfaces/openapi/README|OpenAPI snapshots]]: where the normative OpenAPI document lives.

## The ring

```
                     ┌────────────────────────────┐
                     │        Canonical API       │
                     │     (HTTP/JSON, /v1/...)   │
                     └──────────────┬─────────────┘
                                    │
        ┌───────────────┬───────────┼───────────────┬──────────────────┐
        ▼               ▼           ▼               ▼                  ▼
   in-repo MCP     musubi-sdk   musubi CLI    agent-host plugins   musubi-livekit
   adapter         (PyPI)       (operator)    (musubi-claude,      (voice workers)
   (musubi.sdk)                               -codex, -openclaw,
                                               -opencode, -grok,
                                               -hermes; most on
                                               musubi-harness)
```

Core owns the canonical API, the in-repo SDK, the MCP adapter and the CLI. The other integrations are **independent projects** in sibling repositories with their own release schedules. The list of integrations and where each one lives is in [Connect](../../guide/connect.md).

## Why this separation

See [[03-system-design/abstraction-boundary]]. In short:

- **Core should not know what MCP is.** An adapter maps an MCP tool call to an API call and back.
- **A voice worker should not know about vault sync or promotion.** It consumes retrieval through the API.
- **Integrations move faster than Core.** MCP revisions, LiveKit SDK changes and agent-host plugin APIs should not ripple into Musubi.

## Testing the contract

Core's own coverage lives in `tests/api/`, `tests/sdk/` and `tests/adapters/`, plus OpenAPI drift tests that compare the committed `openapi.yaml` with the running app. Each sibling integration tests itself against its own fixtures. A shared contract suite that every integration runs is future work; see [[07-interfaces/contract-tests]].

## Versioning

- **Canonical API:** path-prefixed. Within `/v1/` changes are additive only; a breaking change needs `/v2/` and a deprecation window during which both run.
- **Clients and integrations:** each has its own SemVer and pins the Core version range it supports.

## Surfaces

1. **HTTP/JSON:** the only wire protocol. Every Core ships it. There is no gRPC service.
2. **Python:** the public `musubi-sdk` package for your own code; the in-repo `musubi.sdk` for code inside Core.
3. **MCP adapter:** in this repository, run as `python -m musubi.adapters.mcp.server` (stdio) or with `sse`.
4. **Agent-host plugins and voice:** sibling repositories, as listed above.

The CLI (`musubi`: `context`, `promote force|reject`, `validate rows`; plus `musubi-context`) is an operator tool in this repository, not an end-user adapter.

## Principles

1. **The API is the only contract.** No integration calls Core internals. Integrations are tested against the API.
2. **No surface-specific logic in Core.** Core does not know about MCP, LiveKit or any agent host.
3. **Structured errors.** Typed error codes with readable messages that are safe to show users.
4. **Idempotent writes.** An optional `Idempotency-Key` header makes every write replay-safe.
5. **Backward compatibility first.** Old API versions stay long enough for integrations to catch up.
