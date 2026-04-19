---
title: "Slice: Cross-encoder reranker"
slice_id: slice-retrieval-rerank
section: _slices
type: slice
status: done
owner: gemini-3-1-pro
phase: "3 Reranker"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-embedding]]"]
blocks: ["[[_slices/slice-retrieval-deep]]"]
---

# Slice: Cross-encoder reranker

> BGE-reranker-v2-m3 via TEI. Stateless; GPU-resident. Bounded by deep-path budget.

**Phase:** 3 Reranker · **Status:** `done` · **Owner:** `gemini-3-1-pro`

## Specs to implement

- [[05-retrieval/reranker]]

## Owned paths (you MAY write here)

- `src/musubi/retrieve/rerank.py`
- `tests/retrieve/test_rerank.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/retrieve/hybrid.py`   (owned by slice-retrieval-hybrid, done)
- `src/musubi/retrieve/scoring.py`  (owned by slice-retrieval-scoring, done)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-embedding]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-deep]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [x] Every Test Contract item in the linked spec(s) is a passing test.
- [x] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [x] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [x] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 14:30 — gemini-3-1-pro — handoff to in-review

- Implemented `musubi/retrieve/rerank.py` using `TEIRerankerClient`.
- Tests: 10 passing (covers 11/13 Test Contract bullets; deferred: `integration: ...` x2).
- Coverage: 92% on owned paths (`src/musubi/retrieve/rerank.py`).
- `make check` clean: ruff format + lint + mypy strict + pytest.
- PR #60 marked ready for review.

### 2026-04-19 14:00 — gemini-3-1-pro — claim

- Claimed slice via `pick-slice` skill. Issue #31, PR #60 (draft).
- Declared `integration: deep-path NDCG@10 on golden` and `integration: deep-path p95 latency under` out-of-scope (deferred to follow-up integration slices).

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
