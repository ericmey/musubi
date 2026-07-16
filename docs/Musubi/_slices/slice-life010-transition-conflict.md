---
title: "Slice: LIFE-010 — Transition Conflict Hard Fence"
slice_id: slice-life010-transition-conflict
status: done
owner: gemini-3-1-pro
phase: "Lifecycle"
section: _slices
type: slice
tags: [section/slices, status/done, type/slice]
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---

# Slice: LIFE-010 — Transition Conflict Hard Fence

Closes #556.

## What

Enforces hard-fence semantic validation for `expected_version` mismatches during lifecycle transitions. Replaces the stale "last writer wins" permissive logging warning with an immediate `version_fence_violation` failure.

## Specs to implement
- [[06-ingestion/index]]

## Definition of Done
- [x] Stale supplied `expected_version` immediately returns `version_fence_violation` Err.
- [x] Zero `coordinator.transition` or `LifecycleEventSink` flushing side-effects.
- [x] Zero state, version, or lineage mutation on conflicting requests.
- [x] Focused tests, full `make check`, and documentation gates exactly passing.

## Files
- `owns_paths`:
  - `docs/Musubi/_slices/slice-life010-transition-conflict.md` (this slice)
  - `docs/Musubi/13-decisions/c6b-lifecycle-atomicity-design.md` (LIFE-010 truth-doc edit: ADR paragraph describes the shipped hard-fence contract)
  - `tests/lifecycle/test_c6b_atomicity.py` (LIFE-010 truth-doc edit: `_R12_REASON` prose updated to reflect the shipped hard-fence contract — no behavioural change)
- `cross-slice borrowed paths` (authorized by Yua fork ruling 2026-07-13, owned by `slice-lifecycle-engine`):
  - `src/musubi/lifecycle/transitions.py` — the hard-fence check at line 192-205 (returns `version_fence_violation` before legality or coordinator apply). The owning slice's work log was updated accordingly.
- `cross-slice borrowed paths` (test additions live alongside existing lifecycle tests, no ownership claim):
  - `tests/lifecycle/test_lifecycle.py` — the discriminator `test_concurrent_transitions_stale_expected_version_fence_violation`.

## Test Contract
1. `test_concurrent_transitions_stale_expected_version_fence_violation`

## Work log
- Implemented `version_fence_violation` strict enforcement in `transitions.py`.
- Rebuilt discriminator asserting strict `Err` format and stdlib `unittest.mock.patch.object` zero-call spies confirming completely suppressed target side-effects over state mutation and `LifecycleEventSink` flushing.
- Resolved Issue #556 exactly. Issue #556 is an authorized cross-slice correction (transitions.py is owned by slice-lifecycle-engine); the owning slice work log was updated accordingly.
