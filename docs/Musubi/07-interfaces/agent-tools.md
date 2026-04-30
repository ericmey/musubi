---
title: Agent Tools — Canonical Surface
section: 07-interfaces
tags: [adapter, agent-tools, interfaces, section/interfaces, status/proposed, type/spec]
type: spec
status: proposed
updated: 2026-04-29
up: "[[07-interfaces/index]]"
reviewed: false
---
# Agent Tools — Canonical Surface

The five tools every Musubi adapter exposes to the agents it hosts. Same names. Same parameter shapes. Same response semantics. Different transports, identical contract.

## Why this exists

Aoi runs across modalities — phone, voice, Discord, Claude Code. The user expects "Aoi, what was I just working on?" to behave the same way regardless of which surface answers. Today it doesn't, because every adapter (`openclaw-musubi`, `openclaw-livekit`, `musubi/adapters/mcp/`) has independently implemented its own agent-tool surface — different names (`musubi_recall` vs `musubi_search` vs `memory_recall`), different parameter shapes, different planes covered. Three implementations, none in sync.

The decision behind this spec is captured in [[13-decisions/0032-agent-tools-canonical-surface]]. This file is the contract every adapter implements.

## The five tools

| Tool | Purpose | Cross-modal by default? |
|---|---|---|
| `musubi_recent` | "What's recent in my world?" — recency-ordered, no query | yes |
| `musubi_search` | "Have I seen X before?" — hybrid + rerank semantic search | yes |
| `musubi_get` | "Tell me more about that one" — fetch a single object's full content by id | n/a (caller-specified) |
| `musubi_remember` | "Save this" — explicit episodic capture | no (writes to caller's presence) |
| `musubi_think` | "Tell my other self" — presence-to-presence message | n/a (caller specifies recipient) |

Adapters MAY also expose lower-level granular tools (per-plane `curated_get`, `thought_history`, etc.) where the surface needs them — but the five above are required and use exactly the names below.

## Naming and shape rules

- **Tool names are canonical.** `musubi_recent`, `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_think`. Adapters do not rename them.
- **Parameter names are canonical** in `snake_case` at the wire/contract layer. Language-idiomatic adapters MAY translate (e.g. `object_id` → `objectId` in TypeScript) but the spec name is the snake_case form.
- **Response is unstructured text** that an LLM reads. A formatted multi-line string, not JSON. Each tool defines a stable layout (header line + body) so model behavior is comparable across adapters.
- **Errors come back as tool errors**, not exceptions. Implementations set the adapter's "tool error" flag (e.g. MCP `isError: true`, OpenClaw plugin `ToolResult.isError`, LiveKit `function_tool` returning the error string).
- **Defaults match.** When two adapters are given the same logical input, they call the same SDK methods with the same flags.

## Tool contracts

### `musubi_recent`

Recent activity in the calling presence's scope. No query. Default scope is **cross-modal** — fans out across every modality the presence has touched (`<tenant>/*/episodic` per [[13-decisions/0031-retrieve-wildcard-namespace]]).

**Parameters**

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `limit` | integer | no | 10 | 1–50. Max results to return. |
| `scope` | enum | no | `cross_modal` | `cross_modal` (`<tenant>/*/episodic`), `presence` (`<tenant>/<presence>/episodic`), `current_modality` (`<tenant>/<presence>/<this-modality>/episodic` if the adapter knows its modality, else falls back to `presence`). |
| `since` | ISO-8601 timestamp | no | none | Inclusive lower bound. Absent = "newest items, ignoring time." |
| `tags` | array of strings | no | none | Filter to rows whose `tags` contains every listed tag. |

**SDK call**: `client.retrieve(namespace=<scope-resolved>, mode="recent", limit=…, since=…, tags=…)` — depends on backend `mode=recent` (see `[[_slices/slice-retrieve-recent]]`). Until that lands, adapters MAY implement a fallback via `client.list_episodic(...)` paginated; the fallback's behavior must match the contract (recency-ordered) and is documented in the adapter spec.

**Response shape**

```
Recent activity ({scope_label}, last {N}):

[{modality}] {created_at} — {one-line content}
[{modality}] {created_at} — {one-line content}
…
```

`{modality}` comes from the row's namespace (segment 2 of `tenant/presence/<modality>/...` if 4-segment; otherwise the presence segment). Empty result returns `No recent activity in {scope_label}.`

**Errors**: backend unavailable → `Couldn't reach memory right now — continuing without it.` (degraded — agent continues).

### `musubi_search`

Hybrid + rerank semantic search across one or more planes. Default scope is **cross-modal episodic** plus **shared curated/concept**. The deep-path tool — slower than the passive prompt supplement, used when the supplement missed.

**Parameters**

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | — | Natural-language query. Min 1 char. |
| `limit` | integer | no | 5 | 1–20. |
| `planes` | array of enum | no | `["episodic", "curated", "concept"]` | Subset of `curated`, `concept`, `episodic`, `artifact`. |
| `scope` | enum | no | `cross_modal` | Same options as `musubi_recent.scope`. |

**SDK call**: `client.retrieve(namespace=<scope-resolved>, query_text=query, mode="deep", planes=…, limit=…)`. Implementations fan out per-plane when needed, dedup by `object_id`, sort by score, slice to `limit`.

**Response shape**

```
{N} result(s) for "{query}":

[{plane}] (score {score}) {namespace}/{object_id} — {title-or-snippet}
{content}

[{plane}] (score {score}) {namespace}/{object_id} — {title-or-snippet}
{content}

…
```

Empty result returns `No memories matched "{query}".`

**Errors**: same degradation message as `musubi_recent`.

### `musubi_get`

Fetch one object's full content + metadata by id. Companion to `musubi_search` — the agent copies `(plane, namespace, object_id)` straight from a result row.

**Parameters**

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `plane` | enum | yes | — | One of `curated`, `concept`, `episodic`, `artifact`. |
| `namespace` | string | yes | — | The namespace from a `musubi_search` row. |
| `object_id` | string | yes | — | The object id from a `musubi_search` row. |

**SDK call**: `client.{plane}.get(namespace=…, object_id=…)`. Plane → endpoint mapping is hard-coded per [[07-interfaces/canonical-api]] (`/v1/curated/{id}`, `/v1/concepts/{id}`, `/v1/episodic/{id}`, `/v1/artifacts/{id}`).

**Response shape**

```
[{plane}] {namespace}/{object_id}

{key}: {value}
{key}: {value}
…

{content-or-body-or-summary}
```

The metadata block prints whichever canonical fields are present (`title`, `state`, `importance`, `event_at`, `vault_path`, `topics`, `tags`, `participants`) — adapters do not need a per-plane schema, just a stable rendering of present fields.

**Errors**: 404 → tool error `Musubi has no {plane} object {id} in namespace {ns}.` Other errors → tool error `Musubi get failed: {message}`.

### `musubi_remember`

Explicit episodic capture into the caller's presence. Higher importance than the passive capture mirror's default (5) so deliberate calls outweigh ambient writes.

**Parameters**

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `content` | string | yes | — | Min 1 char. The thing worth remembering. One fact/observation per call. |
| `importance` | integer | no | 7 | 1–10. |
| `topics` | array of strings | no | `[]` | Topic tags for later filtering. |
| `idempotency_key` | string | no | auto-generated | Override only when the agent has a stable client-side id. |

**SDK call**: `client.episodic.capture(namespace=<presence>/episodic, content=…, importance=…, tags=topics+[<adapter-tag>], idempotency_key=…)`. Adapters MUST add their own modality tag to `tags` (e.g. `src:openclaw-agent-remember`, `src:livekit-voice-remember`) so a downstream `musubi_recent --tags=src:livekit-voice-remember` can filter to a specific modality.

**Response shape**: `Remembered in Musubi episodic ({namespace}) — id {object_id}.`

**Errors**: typed error subclass message (auth/transient/client). Each surfaces a specific user-facing string per the SDK error taxonomy.

### `musubi_think`

Presence-to-presence message. The agent saying "tell my other self that X" — a thought lands in the recipient presence's stream and surfaces in their next turn as inbound context.

**Parameters**

| Name | Type | Required | Default | Notes |
|---|---|---|---|---|
| `to_presence` | string | yes | — | Recipient. Either the canonical `<owner>/<presence>` form or a short alias the adapter resolves (e.g. `aoi` → `eric/aoi`). `all` broadcasts. |
| `content` | string | yes | — | Min 1 char. |
| `channel` | string | no | `default` | Channel within the recipient's inbox. Use `scheduler` for time-boxed reminders. |
| `importance` | integer | no | 5 | 1–10. Priority hint. |

**SDK call**: `client.thoughts.send(namespace=<sender>/thought, from_presence=…, to_presence=…, content=…, channel=…, importance=…)`.

**Response shape**: `Sent to {to_presence}. (id={object_id})`

**Errors**: same taxonomy as `musubi_remember`.

## Cross-cutting requirements

### Presence resolution

Every tool resolves the calling presence the same way: from the adapter's runtime context (OpenClaw `agentId`, LiveKit `AgentConfig`, MCP `session_key` once supported). The presence resolver maps that to `<owner>/<presence>` and the conventional namespaces:

- `<owner>/<presence>/episodic` for episodic reads/writes
- `<owner>/<presence>/thought` for thought sends
- `<owner>/<presence>/artifact` for artifacts
- `<owner>/_shared/curated` and `<owner>/_shared/concept` for shared knowledge reads (also `<owner>/<presence>/curated` for presence-specific curated, when present)

Adapters fail loud (presence-resolution error → tool error) rather than silently using a default namespace.

### Modality tagging

Every `musubi_remember` capture carries a `src:<adapter>-<verb>` tag so `musubi_recent` and `musubi_search` can filter or label by source modality. Required tags:

| Adapter | `src:` tag |
|---|---|
| OpenClaw plugin | `src:openclaw-agent-remember` |
| LiveKit voice | `src:livekit-voice-remember` |
| MCP | `src:mcp-agent-remember` |
| Capture mirror (passive) | `src:openclaw-capture-mirror` (etc.) |

The capture mirror's tag is per-modality; this is how `musubi_recent --tags=src:openclaw-capture-mirror` finds passive captures from a specific surface.

### Cross-modal default

`musubi_recent` and `musubi_search` default to the wildcard tenant scope (`<tenant>/*/episodic`) so an agent on the phone says "what was I just working on?" and gets answers from every modality, not just phone history. Per-modality narrowing is opt-in via `scope=current_modality` or `scope=presence`.

This is the load-bearing behavioral choice: cross-modality is the *expected* default for an agent that exists across modalities. Restricting scope is the deliberate exception.

### Aliases during deprecation

Each adapter ships the existing pre-canonical names as aliases for **one minor release** after canonical lands. The aliases:

- log a deprecation warning (`canonical name musubi_X has superseded legacy name Y`)
- forward to the canonical implementation
- do not appear in tool advertisements

Concretely:

| Legacy name | Canonical | Adapter |
|---|---|---|
| `musubi_recall` | `musubi_search` | openclaw-musubi, livekit-v2-dormant |
| `memory_recall` | `musubi_search` | mcp-adapter |
| `memory_capture` | `musubi_remember` | mcp-adapter |

## Test contract

Every adapter runs the canonical agent-tools contract suite (extends [[07-interfaces/contract-tests]]). A reference implementation lives in `tests/contract/agent_tools/` once shipped. Required cases (one per tool unless noted):

- [ ] **`musubi_recent` — basic.** Capture three rows in three different modality namespaces (`<tenant>/<p>/episodic`, `<tenant>/<p2>/episodic`, …). Call `musubi_recent` with `scope=cross_modal`. All three rows surface, newest-first.
- [ ] **`musubi_recent` — scope narrowing.** Same setup, call with `scope=presence`. Only the rows from the calling presence's namespace surface.
- [ ] **`musubi_recent` — tag filter.** Capture rows with and without `src:adapter-foo`. Call with `tags=["src:adapter-foo"]`. Only tagged rows surface.
- [ ] **`musubi_search` — cross-modal.** Capture a distinctive phrase in `<tenant>/<other-presence>/episodic`. Call `musubi_search` with `scope=cross_modal` and the phrase. Row surfaces.
- [ ] **`musubi_search` — plane filter.** With curated and episodic both holding the query term, `planes=["episodic"]` returns only the episodic row.
- [ ] **`musubi_get` — round-trip.** Capture a row via `musubi_remember`. Call `musubi_search` to get the id. Call `musubi_get` with that `(plane, namespace, object_id)`. Returned content matches the captured content exactly.
- [ ] **`musubi_get` — 404.** Call with an unknown `object_id`. Tool error returned, message names the missing id and namespace.
- [ ] **`musubi_remember` — modality tagging.** Captured row's `tags` contain the adapter's `src:` tag.
- [ ] **`musubi_remember` — idempotency.** Two calls with the same `idempotency_key` and content store one row.
- [ ] **`musubi_think` — round-trip.** Send a thought from presence A to presence B. The thought appears on B's inbound stream within the contract-test timeout.
- [ ] **Degraded mode.** Backend unavailable. Each tool returns a tool error with a user-readable message; the adapter does not raise.
- [ ] **Presence-resolution failure.** Tool called without resolvable presence. Each tool returns a tool error naming the resolution problem.

## Implementation status

| Adapter | Tracking slice | Status |
|---|---|---|
| MCP | `[[_slices/slice-mcp-canonical-tools]]` | proposed |
| OpenClaw plugin | `[[_slices/slice-openclaw-canonical-tools]]` | proposed (extends openclaw-musubi PR #24) |
| LiveKit | `[[_slices/slice-livekit-canonical-tools]]` | proposed |
| Backend `mode=recent` | `[[_slices/slice-retrieve-recent]]` | proposed |

## Related

- [[13-decisions/0032-agent-tools-canonical-surface]] — the decision behind this spec.
- [[07-interfaces/canonical-api]] — the underlying API surface every tool calls.
- [[07-interfaces/mcp-adapter]] — MCP-specific transport notes.
- [[07-interfaces/livekit-adapter]] — LiveKit-specific transport notes.
- [[07-interfaces/openclaw-adapter]] — OpenClaw-specific transport notes.
- [[13-decisions/0031-retrieve-wildcard-namespace]] — the wildcard-namespace primitive that makes cross-modal default possible.
