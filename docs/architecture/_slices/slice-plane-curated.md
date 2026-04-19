---
title: "Slice: Curated knowledge plane"
slice_id: slice-plane-curated
section: _slices
type: slice
status: in-progress
owner: vscode-cc-opus47
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]"]
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-lifecycle-reflection]]", "[[_slices/slice-retrieval-blended]]", "[[_slices/slice-vault-sync]]"]
---
# Slice: Curated knowledge plane

> Topic-first durable facts. Obsidian vault is the store of record; Qdrant is a derived index rebuilt from the vault.

**Phase:** 4 Planes · **Status:** `in-progress` · **Owner:** `vscode-cc-opus47`

## Specs to implement

- [[04-data-model/curated-knowledge]]

## Owned paths (you MAY write here)

- `musubi/planes/curated/`
- `tests/planes/test_curated.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/episodic/`
- `musubi/planes/artifact/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-vault-sync]]
- [[_slices/slice-retrieval-blended]]

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

### 2026-04-19 — vscode-cc-opus47 — claim

- Claimed slice atomically via `gh issue edit 22 --add-assignee @me`. Issue #22, PR #39 (draft).
- Branch `slice/slice-plane-curated` off `v2`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
