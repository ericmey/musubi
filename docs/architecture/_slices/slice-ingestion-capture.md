---
title: "Slice: Capture endpoint"
slice_id: slice-ingestion-capture
section: _slices
type: slice
status: in-progress
owner: vscode-cc-sonnet47
phase: "1 Schema"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-api-v0-write]]"]
blocks: ["[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-livekit]]"]
---

# Slice: Capture endpoint

> Sync write path. Dedup at ingestion similarity threshold. Provisional state; async enrichment downstream.

**Phase:** 1 Schema · **Status:** `in-progress` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[06-ingestion/capture]]

## Owned paths (you MAY write here)

- `src/musubi/ingestion/capture.py`
- `tests/ingestion/test_capture.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/lifecycle/`
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/retrieve/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-plane-episodic]]
- [[_slices/slice-api-v0-write]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-adapter-mcp]]
- [[_slices/slice-adapter-livekit]]

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

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — vscode-cc-sonnet47 — claim

- Claimed slice atomically via `gh issue edit 10 --add-assignee @me`. Issue #10, PR #86 (draft).
- Branch `slice/slice-ingestion-capture` off `v2`.
- Caught the same `owns_paths` `src/`-prefix drift Hana flagged on `slice-retrieval-blended`; operator landed reconcile PR #83 (commit `870bc84`) before this claim — claim made against canonical state.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
