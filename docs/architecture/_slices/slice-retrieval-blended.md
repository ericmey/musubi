---
title: "Slice: Blended multi-plane retrieval"
slice_id: slice-retrieval-blended
section: _slices
type: slice
status: in-progress
owner: gemini-2-0-flash
phase: "5 Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-retrieval-deep]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-retrieval-orchestration]]"]
---

# Slice: Blended multi-plane retrieval

> Single ranked list across planes with de-dup, lineage, provenance weight.

**Phase:** 4 Planes · **Status:** `in-progress` · **Owner:** `gemini-2-0-flash`

## Specs to implement

- [[05-retrieval/blended]]

## Owned paths (you MAY write here)

- `musubi/retrieve/blended.py`
- `tests/retrieve/test_blended.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`

## Depends on

- [[_slices/slice-retrieval-deep]]
- [[_slices/slice-plane-curated]]
- [[_slices/slice-plane-artifact]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-orchestration]]

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

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_

### 2026-04-19 — gemini-2-0-flash — claim

- Claimed slice via Issue #26. Draft PR #79.
