---
title: Agent Tools — Canonical Surface
section: 07-interfaces
tags: [adapter, agent-tools, interfaces, section/interfaces, status/active, type/spec]
type: spec
status: active
updated: 2026-10-01
up: "[[07-interfaces/index]]"
reviewed: false
---
# Agent Tools — Canonical Surface

The five tools a Musubi integration exposes to the agents it hosts: same names, same core parameters, same response semantics across transports.

## Why this exists

An agent that lives on several surfaces (voice, chat, a coding assistant) should answer "what was I just working on?" the same way wherever it is asked. Integrations that each invent their own tool names and shapes (`memory_recall` in one place, `musubi_recall` in another) drift apart. ADR [[13-decisions/0032-agent-tools-canonical-surface]] fixes one surface, and this page is the contract.

## The five tools

| Tool | Purpose |
|---|---|
| `musubi_recent` | "What's recent?": recency-ordered, no query |
| `musubi_search` | "Have I seen X before?": hybrid search with rerank |
| `musubi_get` | "Tell me more about that one": fetch one object by id |
| `musubi_remember` | "Save this": explicit episodic capture |
| `musubi_think` | "Tell my other self": presence-to-presence message |

Integrations may add lower-level tools where their surface needs them, but these five use exactly these names.

## Naming and shape rules

- **Tool names are canonical.** Integrations do not rename them.
- **Parameter names are canonical** in `snake_case`. A TypeScript integration may translate idiomatically (`object_id` to `objectId`), but the spec name is the snake_case form.
- **Responses are text for a model to read**, not JSON. Each tool has a stable layout (a header line, then a body) so model behaviour is comparable across integrations.
- **Errors come back as tool results, not exceptions.** The in-repo MCP adapter returns a string starting with `Error: `; integrations with a tool-error flag (for example MCP `isError`) may set it as well.
- **Defaults match.** The same logical input should produce the same API call.

## Where namespaces come from

Namespaces are `tenant/presence/plane` ([[13-decisions/0030-agent-as-tenant]]): an agent such as Alex is the tenant, and each surface it runs on is a presence (`alex/voice`, `alex/claude-code`, `alex/discord`).

This is where implementations differ:

- **In-repo MCP adapter** (`src/musubi/adapters/mcp/`): `namespace` is an **explicit argument on every tool**. The adapter does not resolve a presence or default the scope. The agent, or the host's system prompt, passes it.
- **Integration plugins** in sibling repositories (`musubi-claude`, `musubi-codex`, `musubi-openclaw`, `musubi-livekit`, `musubi-hermes`, `musubi-opencode`, `musubi-grok`, mostly built on `musubi-harness`): these resolve the agent's presence from their own configuration, may hide `namespace`, and may add a `scope` parameter. Their READMEs are the reference for their exact parameters.

The common rule: a write always targets one concrete 3-segment namespace (`<tenant>/<presence>/<plane>`). Reads may use a 2-segment presence root (cross-plane) or a wildcard such as `alex/*/episodic` (every presence of one agent; [[13-decisions/0031-retrieve-wildcard-namespace]]).

## Tool contracts

Parameters below are the **in-repo MCP adapter's**; it is the reference implementation in this repository.

### `musubi_recent`

Recent activity in a namespace, newest first. No query.

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `namespace` | string | yes | | A 2-segment root returns recent **episodic** rows; a 3-segment namespace selects another plane; a wildcard (`alex/*/episodic`) reads across presences. |
| `limit` | integer | no | 10 | |
| `tags` | array of strings | no | none | AND filter, for example `["src:mcp-agent-remember"]`. |

API call: `POST /v1/retrieve` with `mode="recent"` (shipped). The request also supports `since` (epoch seconds), but the MCP tool does not expose it.

Response:

```
{N} recent in '{namespace}' (newest first):

[{plane}] {YYYY-MM-DD HH:MM}  {namespace}/{object_id} — {title, when present}
{content}
…
```

An empty result returns `No recent activity in '{namespace}'.`

### `musubi_search`

Hybrid search with cross-encoder rerank (`mode="deep"`). Slower than a passive context supplement; use it when the supplement missed.

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `namespace` | string | yes | | 3-segment, 2-segment (cross-plane) or wildcard. |
| `query` | string | yes | | |
| `limit` | integer | no | 5 | |
| `planes` | array | no | none | Subset of `curated`, `concept`, `episodic`, `artifact`; needed with a 2-segment namespace. |

Response:

```
[{plane}] (score {score}) {title, or namespace/object_id when untitled}
{content}
…
```

An empty result returns `No memories matched '{query}'.` A degraded result starts with `[SYSTEM: Retrieval degraded: <codes>]`. Truncated content is marked with its original length.

### `musubi_get`

Fetch one object by id. The agent copies `(plane, namespace, object_id)` from a search row.

| Name | Type | Required | Notes |
|---|---|---|---|
| `plane` | enum | yes | `curated`, `concept`, `episodic`, `artifact` |
| `namespace` | string | yes | Full 3-segment namespace, or the 2-segment root (composed with `plane`) |
| `object_id` | string | yes | |

Plane-to-accessor mapping (the SDK uses singular `episodic`/`curated` and plural `concepts`/`artifacts`, matching the API paths):

| `plane` | SDK accessor | API path |
|---|---|---|
| `curated` | `client.curated.get()` | `GET /v1/curated/{id}` |
| `concept` | `client.concepts.get()` | `GET /v1/concepts/{id}` |
| `episodic` | `client.episodic.get()` | `GET /v1/episodic/{id}` |
| `artifact` | `client.artifacts.get()` | `GET /v1/artifacts/{id}` |

Response: a header `[{plane}] {namespace}/{object_id}`, every non-empty field (canonical ones such as `title`, `state`, `importance`, `event_at`, `vault_path`, `topics`, `tags`, `participants` first, the rest alphabetically), then the content.

### `musubi_remember`

Explicit episodic capture. Default importance is 7, above the passive-capture default of 5, so deliberate saves outweigh ambient ones.

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `namespace` | string | yes | | The 3-segment episodic namespace to write. |
| `content` | string | yes | | One fact or observation per call; at most 32,768 UTF-8 bytes. |
| `importance` | integer | no | 7 | 1–10 |
| `topics` | array of strings | no | `[]` | Folded into `tags`; the capture API has no `topics` field. |

The adapter adds `kind:episode` and `staleness:episodic` when absent, plus its modality tag (see below). The SDK auto-generates an `Idempotency-Key`; the MCP tool does not expose one.

Response: `Remembered in Musubi episodic ({namespace}) — id {object_id}.`

### `musubi_think`

Presence-to-presence message. The thought lands in the recipient's inbox and stream, and surfaces in its next turn.

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `namespace` | string | yes | | The **sender's** 3-segment thought namespace, for example `alex/claude-code/thought`. |
| `from_presence` | string | yes | | |
| `to_presence` | string | yes | | Recipient presence; `all` broadcasts. |
| `content` | string | yes | | |
| `channel` | string | no | `default` | Use `scheduler` for time-boxed reminders. |
| `importance` | integer | no | 5 | 1–10 |

Response: `Sent to {to_presence}. (id={object_id})`

## Modality tagging

Every `musubi_remember` capture carries a `src:<integration>-<verb>` tag, so `musubi_recent` and `musubi_search` can filter or label by source. The in-repo MCP adapter uses `src:mcp-agent-remember`. Each sibling integration defines its own tag in its repository (for example a voice integration's `src:livekit-voice-remember`).

## Deprecated aliases

The in-repo MCP adapter still **registers and advertises** two pre-canonical names, with `[DEPRECATED]` in their descriptions. Each logs a deprecation warning on every call:

| Legacy name | Canonical |
|---|---|
| `memory_capture` | `musubi_remember` (keeps its old default importance of 5) |
| `memory_recall` | `musubi_search` |

They were meant to stay for one minor release; removing them is a pending code change.

## Tests

In this repository, `tests/adapters/test_mcp_canonical_tools.py` covers the MCP adapter's five tools and both aliases: modes, plane routing, 2-segment namespace composition, modality tag, default importance, empty-result and backend-error strings, truncation metadata. `tests/adapters/test_ret007_adapter_warnings.py` covers degradation warnings.

A shared, cross-integration agent-tools suite (one test set every integration runs) does not exist yet; see [[07-interfaces/contract-tests]]. Cases it should cover:

- recent across presences with a wildcard namespace returns rows from each presence, newest first;
- recent with a tag filter returns only tagged rows;
- search with `planes=["episodic"]` excludes curated hits;
- get round-trips a row created by remember, and an unknown id returns a tool error naming the id and namespace;
- remember applies the integration's `src:` tag;
- think from presence A appears in presence B's inbox;
- with the backend down, every tool returns a readable tool error and the integration does not raise.

## Implementation status

| Integration | Where | Status |
|---|---|---|
| MCP adapter | this repository, `src/musubi/adapters/mcp/` | all five tools implemented, plus the two deprecated aliases |
| Retrieve `mode="recent"` | Core API | shipped |
| Agent-host plugins (Claude Code, Codex, OpenClaw, OpenCode, Grok Build, Hermes) | sibling `musubi-*` repositories | see each repository |
| Voice workers | `musubi-livekit` | see that repository |

## Related

- [[13-decisions/0032-agent-tools-canonical-surface]]: the decision behind this spec.
- [[07-interfaces/canonical-api]]: the API every tool calls.
- [[07-interfaces/mcp-adapter]]: the in-repo MCP server.
- [[13-decisions/0031-retrieve-wildcard-namespace]]: the wildcard primitive behind cross-presence reads.
