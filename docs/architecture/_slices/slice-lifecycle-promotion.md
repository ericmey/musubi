---
title: "Slice: Promotion / demotion"
slice_id: slice-lifecycle-promotion
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-nyla
phase: "6 Lifecycle"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-synthesis]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-vault-sync]]"]
blocks: []
---

# Slice: Promotion / demotion

> Threshold-gated write to vault (promotion) + soft-delete flag (demotion). All mutations versioned.

**Phase:** 6 Lifecycle · **Status:** `in-progress` · **Owner:** `gemini-3-1-pro-nyla`

## Specs to implement

- [[06-ingestion/promotion]]
- [[06-ingestion/demotion]]

## Owned paths (you MAY write here)

  - `musubi/lifecycle/promotion.py`
  - `musubi/lifecycle/demotion.py`
  - `tests/lifecycle/test_promotion.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/planes/`
  - `musubi/api/`

## Depends on

  - [[_slices/slice-lifecycle-synthesis]]
  - [[_slices/slice-plane-curated]]
  - [[_slices/slice-vault-sync]]

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

### 2026-04-19 17:30 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #13, PR #68 (draft).

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
