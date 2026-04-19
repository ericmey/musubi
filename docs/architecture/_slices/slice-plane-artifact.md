---
title: "Slice: Source artifact plane"
slice_id: slice-plane-artifact
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-hana
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]"]
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-retrieval-blended]]", "[[_slices/slice-retrieval-orchestration]]"]
---
# Slice: Source artifact plane

> Raw documents, transcripts, logs. Blob storage + Qdrant chunk index. Append-only; versioning via new artifact-id.

**Phase:** 4 Planes · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[04-data-model/source-artifact]]

## Owned paths (you MAY write here)

- `musubi/planes/artifact/`
- `tests/planes/test_artifact.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/episodic/`
- `musubi/planes/curated/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]

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
