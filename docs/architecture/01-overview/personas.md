---
title: Personas
section: 01-overview
tags: [overview, personas, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-04-17
up: "[[01-overview/index]]"
reviewed: false
---
# Personas

## Humans

### Eric (Primary operator)
Single power-user, developer. Edits curated knowledge directly in Obsidian. Runs the Ansible playbooks. Triages lifecycle events. Owns the vault git repo.

### Small-team members (up to ~5 humans)
Use the system via their AI presences (voice, chat, code). May occasionally edit the vault but expect the majority of curated knowledge to be promoted from conversation. Each has a tenant identifier; their episodic memories are namespace-scoped to them.

## Agents (Presences)

Each is an independent AI agent identity, not tied to a specific model. A presence is how Musubi knows *who* is writing/reading memory. Presences are declared in `musubi/config/presences.yaml` and require a tenant binding.

### `claude-code`
Developer-facing coding agent. Primary consumer of Musubi via the MCP adapter. Uses memory for project context, decisions, architectural rationale. Writes episodic on every meaningful exchange. Triggers synthesis regularly.

### `claude-desktop` / `claude-web`
General chat presence. Uses memory for continuity across sessions. Blended retrieval (episodic + curated).

### `livekit-voice` / `livekit-<named-persona>`
Voice agents running on LiveKit. Hard 200ms budget. Fast-path episodic only at turn start; deep retrieval only via explicit tool call.

### `openclaw`
Desktop OpenClaw app presence. Blended retrieval; similar profile to `claude-desktop`.

### `discord-bot`
Async presence monitoring a Discord server. Captures episodic memories per channel. Answers with blended retrieval when mentioned.

### `lifecycle-worker`
Special system presence representing the Lifecycle Engine. Writes promotions, demotions, merges. Appears in audit trails as the author of synthesized/promoted objects.

### `scheduler`
Special system presence for automated jobs (scheduled reflections, backups). Writes thought-type messages for notification.

## Downstream projects

Each adapter is an independent project maintained separately. They depend on `musubi-sdk-py` or `musubi-sdk-ts`.

| Project | Language | Purpose |
|---|---|---|
| `musubi-mcp` | Python | FastMCP server exposing Musubi as MCP tools. Ships `stdio` + `streamable-http` transports. |
| `musubi-livekit` | Python | LiveKit Agents toolkit with pre-built retrieval nodes and fast-path cache. |
| `musubi-openclaw` | TypeScript | OpenClaw desktop-app extension. Reads/writes memory from the desktop context. |
| `musubi-discord` | Python | Discord bot; channel capture + blended responses. |
| `musubi-cli` | Python | Operator CLI. Snapshot, restore, vault reindex, lifecycle run-once, dry-runs. |
| `musubi-studio` | TypeScript (web) | Optional — web UI for browsing lifecycle state, audit log, reflection outputs. Post-v1. |

The MCP, LiveKit, OpenClaw, and REST/gRPC surfaces are the four first-class adapters per [[07-interfaces/index]]. Others ride on the same SDK.
