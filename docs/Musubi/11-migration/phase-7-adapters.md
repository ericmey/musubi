---
title: "Phase 7: Adapters"
section: 11-migration
tags: [adapters, livekit, mcp, migration, openclaw, phase-7, section/migration, status/stub, type/migration-phase]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-6-lifecycle]]"
next: "[[11-migration/phase-8-ops]]"
reviewed: false
---
# Phase 7: Adapters

Break the MCP server out of the monolith into its own adapter. Add LiveKit + OpenClaw adapters. Introduce the Python SDK + canonical HTTP/gRPC API.

## Goal

Musubi Core is pure FastAPI; MCP, LiveKit, OpenClaw are independent repos consuming the SDK. The canonical API becomes first-class.

## Changes

### Canonical HTTP API

Pull the existing FastMCP tools into FastAPI routes:

- `POST /v1/episodic` → capture (was `memory_store` tool).
- `POST /v1/retrieve` → retrieve (was `memory_recall` tool).
- `POST /v1/thoughts/send|check|read` → thoughts.
- Full endpoint list: [[07-interfaces/canonical-api]].

OpenAPI auto-generated from pydantic. Served at `/v1/openapi.json`.

### Bearer auth

Introduce the auth pipeline from [[10-security/auth]]. Until phase 7, tokens were optional; now they're required.

Short transition window: during rollout, both modes are supported (feature flag `MUSUBI_REQUIRE_AUTH=false` default true; set to false during cutover).

### Python SDK

New repo `musubi-client`. See [[07-interfaces/sdk]]. Wraps HTTP calls with typed models + retry.

### MCP adapter

New repo `musubi-mcp-adapter`. See [[07-interfaces/mcp-adapter]]. Runs either stdio or HTTP. MCP tool schemas generated from pydantic models.

POC's `server.py` becomes a thin compatibility shim that imports musubi-mcp-adapter but keeps same tool names, for Claude Code continuity.

### LiveKit adapter

New repo `musubi-livekit-adapter`. See [[07-interfaces/livekit-adapter]]. Python package imported into LiveKit agent workers.

### OpenClaw adapter

New repo `musubi-openclaw-adapter`. See [[07-interfaces/openclaw-adapter]]. TypeScript package embedded into the OpenClaw extension.

### Contract test suite

New repo `musubi-contract-tests`. See [[07-interfaces/contract-tests]]. Each adapter runs it against Musubi Core.

### Kong

Add Kong in front for TLS + rate limits. See [[08-deployment/kong]]. Before this phase, adapters talked directly to `localhost:8100`; now they go through `https://musubi.example.local.example.com`.

## Done signal

- MCP adapter passes contract suite via stdio + HTTP transports.
- LiveKit adapter integrates with a test LiveKit session; Slow Thinker + Fast Talker work.
- OpenClaw adapter captures + retrieves from a real browsing session.
- All three consume SDK; no direct Qdrant calls from adapters.
- Legacy Claude Code session still works via MCP adapter's compatibility shim.

## Rollback

Each adapter is independent; can revert individually. Core still serves the old FastMCP tools via the shim until phase 7.5 cleanup removes them.

If the SDK is broken, adapters can temporarily call HTTP directly.

## Smoke test

```
# MCP via stdio (Claude Code perspective):
> capture: "phase 7 smoke test"
> recall: "phase 7"
# Works.

# MCP via HTTP:
curl -H "Authorization: Bearer $T" -d '{...}' \
  https://musubi.example.local.example.com/v1/episodic

# LiveKit (in a test harness):
# Start voice session → speak → Slow Thinker prefetches → Fast Talker replies with context.

# OpenClaw (in extension):
# Highlight text → "Remember this" → memory appears in retrieval.

# Contract suite:
pytest --contract=canonical --musubi-url=https://musubi.example.local.example.com/v1
```

## Estimate

~3 weeks. Most of that is wiring + testing; each adapter is non-trivial in its own specifics.

## Pitfalls

- **Token minting.** Each adapter needs an OAuth client + test token pipeline. Set up in the auth authority before cutting over.
- **Contract drift.** Before v1.0, the API shape is still malleable. Lock down when promoting to v1 — post-v1 changes require versioning.
- **MCP presence resolution.** The adapter maps incoming tool calls to presences via OAuth `sub` or config. Mis-mapping surfaces as 403s — verify with explicit tests.
- **LiveKit latency.** The dual-agent pattern depends on Slow Thinker having reasonable prefetch latency. On-box GPU helps; monitor.
- **OpenClaw service worker restarts.** Chrome restarts service workers aggressively; tokens must survive via `chrome.storage.local`.
