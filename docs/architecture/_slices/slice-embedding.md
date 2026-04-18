---
title: "Slice: Embedding client layer"
slice_id: slice-embedding
section: _slices
type: slice
status: ready
owner: unassigned
phase: "2 Hybrid"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-config]]"]
blocks: ["[[_slices/slice-ingestion-capture]]", "[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-rerank]]"]
---
# Slice: Embedding client layer

> TEI (BGE-M3 dense, SPLADE++ sparse) + optional Gemini fallback. Named vectors: `{model}_{version}`.

**Phase:** 2 Hybrid · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[06-ingestion/embedding-strategy]]
- [[08-deployment/gpu-inference-topology]]

## Owned paths (you MAY write here)

  - `musubi/embedding/`
  - `tests/test_embedding.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/retrieve/`
  - `musubi/planes/`

## Depends on

  - [[_slices/slice-config]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-retrieval-hybrid]]
  - [[_slices/slice-ingestion-capture]]

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
