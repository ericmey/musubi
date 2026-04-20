---
title: "Slice: MCP adapter"
slice_id: slice-adapter-mcp
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-nyla
phase: "5 Vault"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-sdk-py]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-retrieval-deep]]"]
blocks: []
---

# Slice: MCP adapter

> FastMCP server. `stdio` + `streamable-http` transports. OAuth 2.1 per MCP spec. Preserves legacy POC tool surface.

**Phase:** 5 Vault · **Status:** `in-progress` · **Owner:** `gemini-3-1-pro-nyla`

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

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 23:00 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #4, PR #95 (draft).

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
