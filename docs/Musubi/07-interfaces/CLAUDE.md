---
title: "Agent Rules — Interfaces (07)"
section: 07-interfaces
type: index
status: complete
tags: [section/interfaces, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: true
---

# Agent Rules — Interfaces (07)

Local rules for `src/musubi/api/`, the root `openapi.yaml`, `src/musubi/sdk/` and `src/musubi/adapters/`. Supplements [[CLAUDE]].

## Must

- **Additive change only within a major version.** New endpoints, new optional request fields and new optional response fields are fine. Anything else bumps the API major (`/v1/` to `/v2/`).
- **Keep `openapi.yaml` in step with the routes.** The committed root `openapi.yaml` is the normative contract. `tests/api/test_api_v0_read.py::test_runtime_openapi_matches_committed_paths` fails when runtime paths and committed paths diverge. Changes to `src/musubi/api/` or `openapi.yaml` need an ADR (additive) or a version bump (breaking).
- **Errors are typed.** Every API error uses the envelope `{"error": {"code", "detail", "hint"}}` with a `code` from the fixed set in `src/musubi/api/errors.py`. The SDK and adapters translate it; they never invent new codes.
- **The request id propagates.** Core reads `X-Request-Id` if present, generates one otherwise, and echoes it on the response. The in-repo SDK sends it when the caller supplies one.
- **Cover what you change.** API changes need tests in `tests/api/`; SDK changes in `tests/sdk/`; MCP adapter changes in `tests/adapters/`. There is no shared contract-test package; see [[07-interfaces/contract-tests]].

## Must not

- Add a public endpoint without updating [[07-interfaces/canonical-api]] and `openapi.yaml` in the same PR.
- Put protocol-specific logic (MCP-isms, LiveKit-isms) in `src/musubi/api/`. Adapters own protocol translation.
- Expose Qdrant or internal storage shapes in the public surface. API request and response models are their own pydantic layer.
- Add a second wire protocol (gRPC or anything else) without an ADR.

## API versioning

- URL prefix `/v1/`.
- A breaking change gets a new prefix (`/v2/`); both versions run side by side through a deprecation window.
- ADRs record every API version bump.

## Where each surface lives

| Surface | Where | Notes |
|---|---|---|
| HTTP API | `src/musubi/api/` | Canonical. `curl` and any language can call it directly. |
| Python SDK (in-repo) | `src/musubi/sdk/` | Used by the MCP adapter and tests. See [[07-interfaces/sdk]]. |
| Python SDK (public) | `sourceblender/musubi-sdk` (`musubi_sdk`) | Separate repository and PyPI package. |
| MCP adapter | `src/musubi/adapters/mcp/` | `python -m musubi.adapters.mcp.server [sse]`. See [[07-interfaces/mcp-adapter]]. |
| LiveKit | `sourceblender/musubi-livekit` | Core keeps only the `musubi.adapters.livekit` compatibility shim. |
| Agent-host plugins | `sourceblender/musubi-claude`, `musubi-codex`, `musubi-openclaw`, `musubi-opencode`, `musubi-grok`, `musubi-hermes` | Mostly built on `sourceblender/musubi-harness`. |
| CLI | `src/musubi/cli/` | `musubi context`, `musubi promote force\|reject`, `musubi validate rows`, plus `musubi-context`. An operator tool, not an adapter. |
