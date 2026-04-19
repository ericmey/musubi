---
title: "Slice: Hybrid dense + sparse search"
slice_id: slice-retrieval-hybrid
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "2 Hybrid"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]", "[[_slices/slice-embedding]]"]
blocks: ["[[_slices/slice-retrieval-fast]]", "[[_slices/slice-retrieval-deep]]"]
---

# Slice: Hybrid dense + sparse search

> Qdrant Query API with server-side RRF fusion over named dense + sparse vectors.

**Phase:** 2 Hybrid · **Status:** `in-progress` · **Owner:** `codex-gpt5`

## Specs to implement

- [[05-retrieval/hybrid-search]]

## Owned paths (you MAY write here)

- `musubi/retrieve/hybrid.py`
- `tests/retrieve/test_hybrid.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]
- [[_slices/slice-embedding]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-fast]]
- [[_slices/slice-retrieval-deep]]

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

### 2026-04-19 12:54 — codex-gpt5 — claimed slice

- Claimed Issue #29 and flipped slice frontmatter from `ready` to `in-progress`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
