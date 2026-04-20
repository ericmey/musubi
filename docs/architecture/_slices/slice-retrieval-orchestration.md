---
title: "Slice: Cross-plane orchestration"
slice_id: slice-retrieval-orchestration
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-nyla
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-retrieval-blended]]", "[[_slices/slice-plane-artifact]]"]
blocks: []
---

# Slice: Cross-plane orchestration

> Compound queries: issue subqueries across planes, fuse programmatically. Pipeline-as-code.

**Phase:** 4 Planes · **Status:** `in-progress` · **Owner:** `gemini-3-1-pro-nyla`

## Specs to implement

- [[05-retrieval/orchestration]]

## Owned paths (you MAY write here)

- `src/musubi/retrieve/orchestration.py`
- `tests/retrieve/test_orchestration.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/retrieve/hybrid.py`   (owned by slice-retrieval-hybrid, done)
- `src/musubi/retrieve/scoring.py`  (owned by slice-retrieval-scoring, done)
- `src/musubi/retrieve/rerank.py`   (owned by slice-retrieval-rerank, done)
- `src/musubi/retrieve/fast.py`     (owned by slice-retrieval-fast, done)
- `src/musubi/retrieve/deep.py`     (owned by slice-retrieval-deep, done)
- `src/musubi/retrieve/blended.py`  (owned by slice-retrieval-blended, done)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-retrieval-blended]]
- [[_slices/slice-plane-artifact]]

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

### 2026-04-19 21:00 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #30, PR #87 (draft).

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
