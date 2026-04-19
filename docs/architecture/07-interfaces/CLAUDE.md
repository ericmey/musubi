---
title: Agent Rules — Interfaces
section: 07-interfaces
type: index
status: complete
tags: [section/interfaces, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[07-interfaces/index]]"
reviewed: true
---

# Agent Rules — Interfaces (07)

Local rules for `musubi/api/`, `openapi.yaml`, `proto/`, and all adapter/SDK repos. Supplements [[CLAUDE]].

## Must

- **Additive change only within a major version.** New endpoints, new optional request fields, new optional response fields — fine. Anything else bumps the API major (`/v1/` → `/v2/`).
- **Contract tests gate every adapter.** The canonical API carries a shared contract suite (`musubi-contract-tests`). Every adapter imports and runs it against its local deployment before merge. See [[07-interfaces/contract-tests]].
- **SDK version tracks API major.** `musubi-sdk-py@1.x` implements `/v1/*`. Adapter pins SDK version range.
- **Errors are typed.** Every error returned by the API has a `code` (enum) and `detail` (string). Adapters translate; they never re-invent.
- **Correlation ID propagates.** The API reads `X-Correlation-Id` if present, generates one otherwise. The SDK carries it in every call.

## Must not

- Add a new public endpoint without updating [[07-interfaces/canonical-api]] in the same PR.
- Introduce protocol-specific logic (MCP-isms, LiveKit-isms) into `musubi/api/`. Adapters own protocol translation.
- Expose Qdrant or pydantic internals in the public surface. API types are their own pydantic layer.
- Ship an adapter change without a contract-tests run in its CI.

## API versioning

- URL prefix `/v1/`, proto package `musubi.v1.*`.
- Breaking change → new prefix `/v2/`, new package. Both live side-by-side for a deprecation window (minimum 6 months).
- ADRs record every API-version bump.

## Adapter ownership

| Adapter             | Language   | Responsibility                                                         |
|---------------------|------------|------------------------------------------------------------------------|
| `musubi-mcp`        | Python     | FastMCP over stdio + streamable-HTTP. OAuth 2.1 per spec. Tool mapping. |
| `musubi-livekit`    | Python     | LiveKit Agents toolkit. Fast Talker + Slow Thinker pattern. Hard 200ms. |
| `musubi-openclaw`   | TypeScript | Desktop-app extension. Blended retrieval. Identity proxy.              |
| (`curl` / direct)   | n/a        | REST is a first-class consumer; no translation layer needed.           |

## Related slices

- [[_slices/slice-api-v0-read]] — canonical API (one-at-a-time writer).
- [[_slices/slice-sdk-py]] — Python SDK.
- [[_slices/slice-adapter-mcp]], [[_slices/slice-adapter-livekit]], [[_slices/slice-adapter-openclaw]].
