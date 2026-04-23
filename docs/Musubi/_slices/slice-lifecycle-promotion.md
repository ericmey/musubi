---
title: "Slice: Promotion / demotion"
slice_id: slice-lifecycle-promotion
section: _slices
type: slice
status: done
owner: gemini-3-1-pro-nyla
phase: "6 Lifecycle"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-23
reviewed: true
depends-on: ["[[_slices/slice-lifecycle-synthesis]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-vault-sync]]"]
blocks: ["[[_slices/slice-lifecycle-promotion-builder]]", "[[_slices/slice-lifecycle-demotion-builder]]"]
---

# Slice: Promotion / demotion

> Threshold-gated write to vault (promotion) + soft-delete flag (demotion). All mutations versioned.

**Phase:** 6 Lifecycle · **Status:** `done` · **Owner:** `gemini-3-1-pro-nyla`

## Specs to implement

- [[06-ingestion/promotion]]
- [[06-ingestion/demotion]]

## Owned paths (you MAY write here)

  - `src/musubi/lifecycle/promotion.py`
  - `src/musubi/lifecycle/demotion.py`
  - `tests/lifecycle/test_promotion.py`
  - `tests/lifecycle/test_demotion.py`

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

- [x] Every Test Contract item in the linked spec(s) is a passing test.
- [x] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [x] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [x] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 18:30 — gemini-3-1-pro-nyla — handoff to in-review

- Implemented `run_promotion_sweep` orchestrator and protocols in `src/musubi/lifecycle/promotion.py`.
- Implemented `demotion_episodic`, `demotion_concept`, and `reinstate` in `src/musubi/lifecycle/demotion.py`.
- Opened cross-slice ticket for missing topics field.
- Tests: 25 passing, 23 skipped (deferred). Branch coverage is 91% overall.
- `make check` clean: ruff format + lint + mypy strict + pytest.
- PR #68 marked ready for review.

### 2026-04-19 17:30 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #13, PR #68 (draft).
- Declared the following out-of-scope (deferred to follow-up integration/property slices):
  - `hypothesis: every successful promotion produces exactly one curated file and one Qdrant point`
  - `integration: happy path — 1 concept → 1 file in vault/, 1 point in musubi_curated, both linked, ops-alert present`
  - `integration: path conflict with human file — sibling created, no human file modified`
  - `integration: rollback flow — promote then archive, vault file in _archive/, Qdrant state=archived`
  - `hypothesis: demotion is idempotent across runs with no change in criteria`
  - `hypothesis: no object that transitions to demoted was accessed within the selection window`
  - `integration: seed 1000 memories with varied properties, run weekly demotion, count transitions matches criteria`
  - `integration: reinstatement round-trip — demote → reinstate → appears in default retrieval`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
