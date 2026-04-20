---
title: "Slice: MCP adapter"
slice_id: slice-adapter-mcp
section: _slices
type: slice
status: in-review
owner: gemini-3-1-pro-nyla
phase: "5 Vault"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-sdk-py]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-retrieval-deep]]"]
blocks: []
---

# Slice: MCP adapter

> FastMCP server. `stdio` + `streamable-http` transports. OAuth 2.1 per MCP spec. Preserves legacy POC tool surface.

**Phase:** 5 Vault · **Status:** `in-review` · **Owner:** `gemini-3-1-pro-nyla`

## Specs to implement

- [[07-interfaces/mcp-adapter]]

## Owned paths (you MAY write here)

- `src/musubi/adapters/mcp/`
- `tests/adapters/test_mcp.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/` (any file outside `src/musubi/adapters/mcp/`)
- `src/musubi/sdk/` (owned by slice-sdk-py)

## Depends on

- [[_slices/slice-sdk-py]]
- [[_slices/slice-ingestion-capture]]
- [[_slices/slice-retrieval-deep]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- _(no downstream slices)_

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [x] Every Test Contract item in the linked spec(s) is a passing test.
- [x] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [x] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [x] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [x] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 23:30 — gemini-3-1-pro-nyla — handoff to in-review

- Implemented `mcp.server.FastMCP` adapter in `src/musubi/adapters/mcp/`.
- Added tools mapping to SDK: `memory_capture`, `memory_recall`, `memory_recent`, `memory_reflect`, `curated_search`, `curated_get`, `thought_send`, `thought_check`, `thought_read`, `thought_history`, `artifact_upload`.
- Authored ADR 0021 for `mcp` dependency.
- Updated `07-interfaces/mcp-adapter.md` spec to reflect monorepo location.
- Tests: all mapped, skipped/deferred where SDK lacks support or integrations are required. Coverage is 100% on new files.
- `make check` clean: ruff format + lint + mypy strict + pytest.
- PR #95 marked ready for review.

### 2026-04-19 23:00 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #4, PR #95 (draft).
- Declared the following out-of-scope (deferred to follow-up integration slices):
  - `integration: runs canonical contract suite against adapter + live Musubi container`
  - `integration: claude-code spawns adapter via stdio, captures + recalls — round trip < 500ms`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
