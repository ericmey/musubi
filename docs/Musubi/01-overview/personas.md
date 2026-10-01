---
title: Personas
section: 01-overview
tags: [overview, personas, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-10-01
up: "[[01-overview/index]]"
reviewed: false
---
# Personas

## Humans

### Admin (primary operator)
The person who runs the deployment: a power user and developer. Prepares the host and runs the Docker Compose stack. Edits curated knowledge directly in Obsidian. Triages lifecycle events. Owns the knowledge vault and its backups.

### Small-team members (up to ~5 humans)
Use the system via their AI presences (voice, chat, code). May occasionally edit the vault but expect the majority of curated knowledge to be promoted from conversation. Musubi namespaces memory by agent, not by person ([[13-decisions/0030-agent-as-tenant|ADR 0030]]): what a team member says is captured under the agent and presence they used, and token scopes decide which agents' memory each client can reach.

## Agents and presences

The tenant is the agent (for example `alex` or `sam`); a presence is the channel or client that agent speaks through (`voice`, `discord`, `claude-code`). Musubi knows *who* is writing or reading from the bearer token: its `presence` claim names `tenant/presence` (for example `alex/voice`) and must equal the token's `sub` (`src/musubi/auth/tokens.py`). There is no presence registry file; a presence exists when a token names it.

Typical presence profiles:

### Coding agent (for example `alex/claude-code`)
Developer-facing coding agent. Consumes Musubi through the MCP server. Uses memory for project context, decisions and architectural rationale. Writes episodic memories on meaningful exchanges.

### Chat presence (for example `alex/desktop`)
General chat presence. Uses memory for continuity across sessions. Blended retrieval (episodic + curated).

### Voice agent (for example `alex/voice`)
Voice agent running on LiveKit. Tight latency budget: the fast path has a 400 ms whole-call budget by default (`RETRIEVAL_FAST_WHOLE_TIMEOUT_S`). Fast-path retrieval at turn start; deep retrieval only via explicit tool call.

### Chat bot (for example `sam/discord`)
Async presence in a chat server. Captures episodic memories. Answers with blended retrieval when mentioned.

### `lifecycle-worker`
The lifecycle worker writes as its own tenant (for example `lifecycle-worker/ops/curated` for reflections) and emits thoughts from the `lifecycle-worker` presence. It appears in lifecycle events as the actor for maturation, promotion and demotion.

## Clients and integrations

In this repo:

| Component | Language | Purpose |
|---|---|---|
| `src/musubi/sdk` | Python | Client SDK, also published as `sourceblender/musubi-sdk`. |
| `src/musubi/adapters/mcp` | Python | MCP server (stdio or SSE) exposing `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_think` and `musubi_recent`. |
| `musubi` CLI | Python | `musubi context`, `musubi promote force`, `musubi promote reject`, `musubi validate`; plus `musubi-context`. |
| `src/musubi/adapters/livekit` | Python | Compatibility shim; the LiveKit integration itself is `sourceblender/musubi-livekit`. |

Separate repositories, each calling the canonical HTTP API: `sourceblender/musubi-livekit`, `musubi-openclaw`, `musubi-claude`, `musubi-codex`, `musubi-hermes`, `musubi-opencode`, `musubi-grok` and `musubi-harness`. See [[07-interfaces/index]].
