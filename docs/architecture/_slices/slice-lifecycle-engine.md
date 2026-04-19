---
title: "Slice: Lifecycle scheduler"
slice_id: slice-lifecycle-engine
section: _slices
type: slice
status: in-progress
owner: cowork-auto
phase: "6 Lifecycle"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-lifecycle-maturation]]", "[[_slices/slice-lifecycle-synthesis]]", "[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-lifecycle-reflection]]"]
---

# Slice: Lifecycle scheduler

> APScheduler-based worker. Emits LifecycleEvents. Idempotent per-job. Separate process from the API.

**Phase:** 6 Lifecycle · **Status:** `in-progress` · **Owner:** `cowork-auto`

## Specs to implement

- [[06-ingestion/lifecycle-engine]]
- [[04-data-model/lifecycle]]

## Owned paths (you MAY write here)

  - `musubi/lifecycle/engine.py`
  - `musubi/lifecycle/states.py`
  - `tests/lifecycle/test_engine.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/api/`

## Depends on

  - [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-lifecycle-maturation]]
  - [[_slices/slice-lifecycle-synthesis]]
  - [[_slices/slice-lifecycle-promotion]]
  - [[_slices/slice-lifecycle-reflection]]

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
