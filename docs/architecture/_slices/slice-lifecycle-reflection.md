---
title: "Slice: Reflection job"
slice_id: slice-lifecycle-reflection
section: _slices
type: slice
status: in-progress
owner: vscode-cc-sonnet47
phase: "6 Lifecycle"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-engine]]", "[[_slices/slice-plane-curated]]"]
blocks: []
---

# Slice: Reflection job

> Daily/weekly narrative digest. Writes to `vault/reflections/`. Read by operator + lifecycle-worker presence.

**Phase:** 6 Lifecycle · **Status:** `in-progress` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[06-ingestion/reflection]]

## Owned paths (you MAY write here)

- `musubi/lifecycle/reflection.py`
- `tests/lifecycle/test_reflection.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`

## Depends on

- [[_slices/slice-lifecycle-engine]]
- [[_slices/slice-plane-curated]]

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

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — vscode-cc-sonnet47 — claim

- Claimed slice atomically via `gh issue edit 14 --add-assignee @me`. Issue #14, PR #57 (draft).
- Branch `slice/slice-lifecycle-reflection` off `v2`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
