---
title: "Slice: Episodic plane"
slice_id: slice-plane-episodic
section: _slices
type: slice
status: ready
owner: unassigned
phase: "4 Planes"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]"]
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-lifecycle-maturation]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-retrieval-fast]]"]
---
# Slice: Episodic plane

> Source-first time-indexed recollection. Qdrant-primary. Named dense + sparse vectors. Provisional → matured lifecycle.

**Phase:** 4 Planes · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[04-data-model/episodic-memory]]

## Owned paths (you MAY write here)

- `musubi/planes/episodic/`
- `tests/planes/test_episodic.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/curated/`
- `musubi/planes/artifact/`
- `musubi/api/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-fast]]
- [[_slices/slice-lifecycle-maturation]]

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
