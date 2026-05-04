---
title: "Slice: MCP adapter — canonical agent tools"
slice_id: slice-mcp-canonical-tools
section: _slices
type: slice
status: done
owner: aoi-claude-opus
phase: "8 Post-1.0"
tags: [section/slices, status/done, type/slice, adapter, mcp, agent-tools]
updated: 2026-04-29
reviewed: false
depends-on: ["[[_slices/slice-adapter-mcp]]", "[[_slices/slice-retrieve-recent]]"]
blocks: []
---

# Slice: MCP adapter — canonical agent tools

> Implement the canonical agent-tools surface in `src/musubi/adapters/mcp/`. Five tools — `musubi_recent`, `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_think` — replacing the v1.0 `memory_capture` / `memory_recall`. Includes registering the MCP server with Claude Code so coding agents have the tools.

**Phase:** 8 Post-1.0 · **Status:** `done` (canonical 4 tools + 2 deprecation aliases shipped; `musubi_recent` is a clearly-deferred stub awaiting [[_slices/slice-retrieve-recent]]) · **Owner:** `aoi-claude-opus`

## Why this slice exists

Per [[13-decisions/0032-agent-tools-canonical-surface]], every adapter exposes the same five canonical tools. The MCP adapter currently exposes only the legacy `memory_capture` + `memory_recall`. This slice brings it onto the canonical contract so Claude Code (and any other MCP client) gets the same surface as Aoi's voice and OpenClaw modalities.

## Specs to implement

- [[07-interfaces/agent-tools]] (the contract; every tool in this slice conforms to it)
- [[07-interfaces/mcp-adapter]] (spec-update trailer: legacy table → canonical table, already done in PR for [[13-decisions/0032-agent-tools-canonical-surface]])

## Owned paths (you MAY write here)

- `src/musubi/adapters/mcp/tools.py` — register all five canonical tools + alias path for `memory_capture`/`memory_recall`
- `src/musubi/adapters/mcp/server.py` — minor: ensure new tools are attached
- `tests/adapters/test_mcp.py` — contract tests per [[07-interfaces/agent-tools#test-contract]] §
- `tests/adapters/test_mcp_canonical_tools.py` — new file, dedicated suite for the canonical surface

## Forbidden paths

- `src/musubi/sdk/` — no SDK changes; this slice consumes existing SDK methods
- `src/musubi/api/` — canonical API frozen per ADR/CLAUDE rules; backend changes for `mode=recent` are owned by [[_slices/slice-retrieve-recent]]
- `src/musubi/types/` — no new types

## Depends on

- [[_slices/slice-adapter-mcp]] — base MCP adapter infrastructure (status: done)
- [[_slices/slice-retrieve-recent]] — `musubi_recent` requires `mode=recent`. Until that lands, this slice MAY ship `musubi_recent` with a fallback (`client.list_episodic` paginated) and migrate when the backend mode lands. The fallback is documented in the adapter spec.

## Unblocks

- _(no downstream slices)_ — once shipped, Claude Code has parity with voice + OpenClaw modalities.

## Test Contract

Implements the canonical contract test suite from [[07-interfaces/agent-tools#test-contract]]. All cases below run against an in-process Musubi (or a contract-test fixture):

- [ ] `musubi_recent` — basic. Three rows in three modality namespaces. `scope=cross_modal` returns all three, newest-first.
- [ ] `musubi_recent` — scope narrowing. `scope=presence` returns only the calling presence's rows.
- [ ] `musubi_recent` — tag filter. `tags=["src:foo"]` filters to tagged rows.
- [ ] `musubi_search` — cross-modal. Distinctive phrase in another modality's episodic surfaces.
- [ ] `musubi_search` — plane filter. `planes=["episodic"]` excludes curated even when curated has the term.
- [ ] `musubi_get` — round-trip. `musubi_remember` → `musubi_search` → `musubi_get` returns the original content exactly.
- [ ] `musubi_get` — 404. Unknown id returns tool error naming the missing id + namespace.
- [ ] `musubi_remember` — modality tagging. Captured row has `src:mcp-agent-remember` in tags.
- [ ] `musubi_remember` — idempotency. Same idempotency_key + content stores one row.
- [ ] `musubi_think` — round-trip. Thought from A to B appears on B's stream within timeout.
- [ ] **Aliases.** `memory_capture` invocation forwards to `musubi_remember` with a deprecation warning. `memory_recall` → `musubi_search` likewise. Both emit a deprecation log line.
- [ ] **Degraded mode.** Backend unavailable. Each tool returns a tool error string; no exception escapes the tool boundary.
- [ ] **Presence-resolution failure.** Tool called without resolvable presence. Tool error names the resolution problem.

## Wiring with Claude Code (operator step, not code)

After the slice merges, the operator runs:

```bash
claude mcp add -s user musubi -- python -m musubi.adapters.mcp serve
```

(or whatever the canonical `musubi mcp serve` invocation is at the time). This is documented in the [[07-interfaces/mcp-adapter]] adapter spec under "Local install". Not code in this slice's commits, but listed here so the slice is genuinely "done" only after Claude Code can call the new tools.

## Definition of Done

![[00-index/definition-of-done]]
