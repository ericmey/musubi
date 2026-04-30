---
title: "ADR 0032: Canonical Agent-Tools Surface"
section: 13-decisions
tags: [adapters, adr, agent-tools, architecture, section/decisions, status/proposed, type/adr]
type: adr
status: proposed
updated: 2026-04-29
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0032: Canonical Agent-Tools Surface

**Status:** proposed
**Date:** 2026-04-29
**Deciders:** Eric

## Context

Musubi exists across modalities. The same logical agent — Aoi — runs on the phone (OpenClaw), in voice calls (LiveKit), in OpenClaw chat sessions, and (eventually, when wired) in Claude Code (MCP). The user expects that saying "Aoi, what was I just working on?" behaves identically regardless of which modality answers, because to the user there is one Aoi.

This is not how it works today. Each adapter has independently defined its agent-tool surface:

| Adapter | Implements | Tools exposed |
|---|---|---|
| `openclaw-musubi` (TS) | OpenClaw plugin | `musubi_recall`, `musubi_remember`, `musubi_think` |
| `openclaw-livekit` (Python, **active path**) | LiveKit voice | `musubi_recent`, `musubi_search`, `musubi_remember` |
| `openclaw-livekit` (Python, **dormant v2 path**) | LiveKit voice | `musubi_recall`, `musubi_remember`, `musubi_think` |
| `musubi/adapters/mcp/` (Python) | MCP server | `memory_capture`, `memory_recall` |

Three observations about the current state:

1. **Tool names diverge.** "Search" is `musubi_recall` in two surfaces, `musubi_search` in one, `memory_recall` in another. "Write" is `musubi_remember` in three surfaces, `memory_capture` in one. Same intent, four different tool calls.
2. **Tool sets diverge.** The voice agent has `musubi_recent` (recency-anchored). No other adapter does. The OpenClaw plugin has `musubi_think`. The MCP adapter has neither. Aoi Phone literally cannot answer "what was I just doing on Claude Code" because the cross-modal recent tool doesn't exist on her side.
3. **A comment in `memory.py:307` already documents this fragility:** _"Tool name + parameter shape match the openclaw-musubi plugin's `musubi_remember` so saves on either surface look the same in traces and to the model."_ The team has been keeping parity by hand. That doesn't scale.

The user-facing symptom is real: when Aoi Phone was asked about "recent activity across modalities," she truthfully reported that her recent-tool only sees phone history. When she was asked to drill into a search snippet, she truthfully reported that her tools only return summaries. Both gaps are functions of which tools her adapter happened to wire.

## Decision

**Define a canonical agent-tools surface — five tools — that every Musubi adapter implements identically.** The contract lives at [[07-interfaces/agent-tools]].

The five tools:

1. `musubi_recent` — recent activity, recency-ordered, **cross-modal by default**.
2. `musubi_search` — hybrid + rerank semantic search, **cross-modal by default**.
3. `musubi_get` — fetch one object's full content + metadata by id.
4. `musubi_remember` — explicit episodic capture into the calling presence.
5. `musubi_think` — presence-to-presence message.

Adapters MAY also expose lower-level granular tools (per-plane writes, ops introspection, etc.) where the surface needs them — but the five above are required, named exactly as specified, and parameter-compatible across adapters.

The contract is written once in the spec; every adapter passes a shared contract test suite that exercises the surface. A new tool starts with a spec change and an ADR amendment, not an adapter PR.

### Cross-modal as default

`musubi_recent` and `musubi_search` default to scope `cross_modal` — `<tenant>/*/episodic` per [[13-decisions/0031-retrieve-wildcard-namespace]]. Per-modality narrowing is opt-in via `scope=presence` or `scope=current_modality`. This is the load-bearing behavioral choice: an agent that exists across modalities should answer "what was I doing" by *looking everywhere*. Restricting scope is the deliberate exception.

### Naming choice — `musubi_search`, not `musubi_recall`

"Recall" implies the agent (or character) is the subject doing the remembering. The character Aoi recalls; the system she runs on searches a database. `musubi_search` names the actual mechanism. The voice-active path already uses this naming; openclaw-musubi's `musubi_recall` is the legacy that diverged first.

### Naming choice — `musubi_*`, not `memory_*`

The MCP adapter's existing `memory_capture` / `memory_recall` use a shorter prefix. We unify on `musubi_*` because:

- Every other surface already uses `musubi_*`. Reversing all of them is more churn than reversing the MCP adapter.
- The prefix tells the agent which memory store the call goes to, which matters when adapters might compose multiple tool sources (e.g. a `memory_*` tool from a different MCP server).

### Deprecation path, not breaking change

Each adapter keeps the old names as aliases for **one minor release** after canonical lands. Aliases:

- log a deprecation warning on each invocation
- forward to the canonical implementation
- do not appear in tool advertisements

After one release, aliases drop. Concrete mapping table is in the spec.

## Alternatives considered

### Alternative 1: Define tools at the SDK level, generate adapters

A code-generation pipeline that takes a single tool definition (params, behavior) and emits MCP, OpenClaw, and LiveKit registrations. Tempting because it would prevent drift mechanically.

**Rejected** for now. Three blockers:

- Each adapter SDK has different ergonomics — MCP's `@mcp.tool` decorator, OpenClaw's `registerTool` factory, LiveKit's `@function_tool` — that don't map to a single template without losing surface-specific behavior (auth context, structured returns, run-time injection).
- Code generation is one more pipeline to maintain. The contract is small (5 tools) and changes slowly.
- A specification-and-contract-tests approach catches drift just as effectively without the build complexity.

If the tool count grows past ~15 or drift becomes recurring, revisit.

### Alternative 2: Keep modality-specific tool sets; document the divergence

Accept that voice has `musubi_search`, plugin has `musubi_recall`, MCP has `memory_recall` — they're functionally equivalent and agents adapt. Document the mapping prominently.

**Rejected.** Three problems:

- Every system prompt has to teach Aoi which tool name applies on each modality. Prompts then drift modality-by-modality.
- "Functionally equivalent" is not actually true today: parameter shapes differ, default scopes differ, response formats differ. Calling them equivalent papers over real bugs.
- Cross-modal continuity (the user's actual goal) requires the model to recognize that "what I told voice-Aoi" and "what plugin-Aoi captured" live in the same memory. A common tool surface makes that obvious; divergent surfaces hide it.

### Alternative 3: Adopt MCP as the canonical wire for every adapter

Run a Musubi MCP server, point every adapter at it, drop the per-adapter tool implementations. One transport, one surface.

**Rejected.** MCP is a coding-agent protocol; LiveKit voice agents and OpenClaw plugins have their own runtime contracts (low-latency `@function_tool`, browser-side `registerTool`) that MCP doesn't replace. MCP can be *one* of the adapters — it already is — but it can't subsume the others without dragging MCP transport overhead into voice and browser-extension contexts.

### Alternative 4: Eight-to-fifteen tools, more granular

Keep finer-grained tools (per-plane `curated_get`, `concept_search`, `thought_history`, `artifact_get`, `episodic_archive`, etc.) — closer to the existing planned MCP surface in [[07-interfaces/mcp-adapter]].

**Rejected as the canonical agent surface.** The agent doesn't typically need plane-level granularity; it needs to *find* and *use* memory. Five high-level tools (`recent`, `search`, `get`, `remember`, `think`) cover ≥95% of what an agent does. Granular per-plane tools are a power-user surface that adapters MAY expose alongside the canonical five — they're optional, not required.

## Consequences

### Good

- **Cross-modal continuity becomes default.** "Aoi, what was I just doing?" works on every modality.
- **One mental model for the user, one mental model for the LLM.** Same tool names everywhere; same behavior. System prompts simplify.
- **Drift becomes a contract-test failure**, not a guess-when-it-breaks. Adding a tool to one adapter without updating the others fails CI.
- **Closes the explicit gaps** users have hit: cross-modal `musubi_recent`, drill-into-source `musubi_get`.
- **Documents the design philosophy** — adapter agnostic, contract-driven — so future adapters (Discord agent, Slack bot, mobile client) start from the same baseline.

### Bad

- **One-time migration cost** across three adapters. Roughly 5 PRs (1 spec + ADR, 1 backend slice for `mode=recent`, 3 adapter slices). Authoring + review effort is real.
- **Existing system prompts** mentioning `musubi_recall` or `memory_recall` need updating. Aliases buffer this for one release; after that, prompts must use the canonical names.
- **External integrations** (if any third-party agent has been built against `memory_recall`) break after the deprecation window. Acceptable: those tools are alpha-grade.
- **The MCP adapter spec ([[07-interfaces/mcp-adapter]]) shifts shape** — granular per-plane tools that were "planned" now mostly aren't. We replace those tables with a reference to the canonical surface and call out the granular tools as optional/future.

### Neutral

- **`musubi_get` parameter requires `plane` explicitly** rather than inferring it from the namespace. Slightly more verbose in calls, but unambiguous and never depends on namespace shape — agents copy the triple from a `musubi_search` row that already shows it.
- **Cross-modal default for `musubi_recent`** widens scope vs the current voice-only `musubi_recent` behavior. Side effect: a voice agent now sees plugin + Discord + Claude-Code captures by default. That's the intended behavior for the cross-modal vision; agents that *want* voice-only can pass `scope=current_modality`.

## Implementation order

1. **This ADR + the spec** ([[07-interfaces/agent-tools]]) — gates everything below.
2. **Backend slice `mode=recent`** ([[_slices/slice-retrieve-recent]]) — required by `musubi_recent` cross-modal. Adapters can ship the canonical names with a fallback recency path until this lands; the fallback is documented per adapter.
3. **MCP adapter slice** ([[_slices/slice-mcp-canonical-tools]]) — adds the five canonical tools, deprecates `memory_capture` / `memory_recall`. Includes registering the MCP server with Claude Code.
4. **OpenClaw plugin extension** — extends the in-flight openclaw-musubi PR (`#24`) to add `musubi_recent` + alias `musubi_recall` → `musubi_search`.
5. **LiveKit collapse** ([[_slices/slice-livekit-canonical-tools]]) — single canonical mixin, widens `musubi_recent` to cross-modal, drops the dormant v2 mixin.

## Related

- [[07-interfaces/agent-tools]] — the contract this ADR backs.
- [[07-interfaces/mcp-adapter]] — affected; granular per-plane tables become future/optional.
- [[07-interfaces/livekit-adapter]] — affected.
- [[07-interfaces/openclaw-adapter]] — affected.
- [[13-decisions/0011-canonical-api-and-adapters]] — the broader interface-discipline ADR this builds on.
- [[13-decisions/0031-retrieve-wildcard-namespace]] — provides the wildcard namespace primitive cross-modal scope depends on.
