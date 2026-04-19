---
title: "Slice: Maturation job"
slice_id: slice-lifecycle-maturation
section: _slices
type: slice
status: in-progress
owner: vscode-cc-sonnet47
phase: "6 Lifecycle"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-engine]]", "[[_slices/slice-plane-episodic]]"]
blocks: ["[[_slices/slice-lifecycle-synthesis]]"]
---

# Slice: Maturation job

> Hourly sweep. Importance scoring (Qwen2.5-7B), tag normalization, dedup pass. Provisional → matured.

**Phase:** 6 Lifecycle · **Status:** `in-progress` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[06-ingestion/maturation]]

## Owned paths (you MAY write here)

- `musubi/lifecycle/maturation.py`
- `tests/lifecycle/test_maturation.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`
- `musubi/api/`

## Depends on

- [[_slices/slice-lifecycle-engine]]
- [[_slices/slice-plane-episodic]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-lifecycle-synthesis]]

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

- Claimed slice atomically via `gh issue edit 12 --add-assignee @me`. Issue #12, PR #52 (draft).
- Branch `slice/slice-lifecycle-maturation` off `v2`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
