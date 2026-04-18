---
title: Core Abstraction Boundary
section: 03-system-design
tags: [architecture, boundary, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[03-system-design/index]]"
reviewed: false
---
# Core Abstraction Boundary

> Musubi's value as a *platform* depends entirely on where the line between "things Musubi owns" and "things callers own" is drawn. This document pins that line.

## The boundary, stated

**Musubi Core owns:**
1. The canonical data model (schemas for all planes).
2. All Qdrant writes and reads. No client talks to Qdrant directly.
3. The vault filesystem layout and frontmatter schema. No client writes vault files directly.
4. The canonical API (HTTP + gRPC), versioned.
5. Authorization decisions (tenant/presence scoping).
6. Lifecycle state transitions.
7. Retrieval scoring.

**Clients (adapters) own:**
1. Their wire protocol (MCP, LiveKit tools, OpenClaw extension interface, raw HTTP).
2. Rendering results for their surface (e.g., the MCP adapter formats results as MCP tool responses; the LiveKit adapter surfaces them as agent-context turns).
3. Caching decisions specific to their latency budget.
4. User experience (e.g., how to present a "no results" state).
5. Authentication at the edge (bearer token provisioning, refresh).
6. Their own retry / back-off logic on network errors.

**Neither owns:**
1. The LLM. Adapters call LLMs directly; Musubi does not route through one in the hot path.
2. Conversation state. Chat history lives in the adapter; Musubi stores distilled episodic memories.
3. Agent orchestration. Musubi serves memory to agents; it does not run them.

## Why this boundary

The guiding principle is **Musubi is the single source of truth for the shape, state, and contents of memory. It is not the interface between a user and memory.** That separation makes everything else fall into place:

- Adding a new surface (say, a terminal TUI, a Telegram bot, an email-triage agent) means building a new adapter. It does not require touching Musubi Core.
- Changing a protocol version (MCP 1.x → 2.x) is an adapter change; Musubi's canonical API is stable across it.
- Multiple adapters using the same memory namespace *see consistent state* because they all mutate through Musubi.

## What crosses the boundary

Only these shapes cross into Musubi Core:

- **Canonical API requests** — typed by the OpenAPI / proto contract.
- **Authentication tokens** — bearer tokens with tenant/presence scope claims.
- **Object store writes** (artifacts) — through the API's `POST /v1/artifacts` endpoint with multipart or pre-signed URL; clients do not write directly to disk.

Nothing else. In particular:

- Adapters do **not** query Qdrant directly even if they have network access. (Qdrant's port is bound to the Docker network, not exposed to the host.)
- Adapters do **not** write to the vault directly, even though the vault is a filesystem. Musubi Core is the sole writer (for `musubi-managed: true` files). Humans edit via Obsidian, which writes files directly — but those are `musubi-managed: false` or human-authored files, and the Vault Watcher picks them up.

## Versioning

The boundary is the API. The API is versioned:

- Contract at `/v1/...` — frozen. Additive changes allowed (new optional fields, new endpoints).
- Breaking changes require `/v2/...`, maintained in parallel for at least one minor version of Musubi Core.
- SDK major version tracks API major version.

## What if a client needs something the API doesn't expose?

Three options, in order of preference:

1. **Propose an additive API change.** Most clients' needs map to a new endpoint or a new optional parameter. Open an ADR, get it approved, one `api-v*` slice implements it.
2. **Do it client-side over the existing API.** If it's composable from existing endpoints (e.g., "fetch episodic + curated, rerank with my own reranker"), keep it in the client. This is fine — it's how specialization should work.
3. **Plugin.** Post-v1 only. If a category of clients needs a hook (custom chunker, custom extractor), we may add a plugin interface to Lifecycle Worker. Not in scope for v1.

What is **never** allowed:

- Direct Qdrant access from a client.
- Direct vault writes from a client.
- Out-of-band mutation of Musubi state (e.g., a shell script inserting Qdrant points).

## Test: the boundary is healthy if

- You can deploy a new adapter without touching Core.
- You can swap Qdrant for a different vector DB without changing any adapter.
- You can upgrade the canonical API minor version without changing any adapter.
- No adapter README contains a note like "Before using this, run this migration on Qdrant."

These are the smoke tests for the abstraction boundary.
