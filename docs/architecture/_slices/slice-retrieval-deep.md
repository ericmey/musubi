---
title: "Slice: Deep-path retrieval"
slice_id: slice-retrieval-deep
section: _slices
type: slice
status: ready
owner: unassigned
phase: "3 Reranker"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]", "[[_slices/slice-retrieval-rerank]]"]
blocks: ["[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-retrieval-blended]]", "[[_slices/slice-retrieval-orchestration]]"]
---
# Slice: Deep-path retrieval

> Full hybrid + cross-encoder rerank. Milliseconds-to-seconds budget. Default for chat/code presences.

**Phase:** 3 Reranker · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[05-retrieval/deep-path]]

## Owned paths (you MAY write here)

- `musubi/retrieve/deep_path.py`
- `tests/retrieve/test_deep_path.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`

## Depends on

- [[_slices/slice-retrieval-hybrid]]
- [[_slices/slice-retrieval-scoring]]
- [[_slices/slice-retrieval-rerank]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-blended]]
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
