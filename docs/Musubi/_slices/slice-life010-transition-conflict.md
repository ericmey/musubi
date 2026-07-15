---
title: "Slice: LIFE-010 — Transition Conflict Hard Fence"
slice_id: slice-life010-transition-conflict
status: in-review
owner: gemini-3-1-pro
phase: "Lifecycle"
section: _slices
type: slice
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-15
reviewed: false
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
  - `docs/Musubi/_slices/slice-life010-transition-conflict.md`

## Test Contract
1. `test_concurrent_transitions_stale_expected_version_fence_violation`

## Work log
- Implemented `version_fence_violation` strict enforcement in `transitions.py`.
- Rebuilt discriminator asserting strict `Err` format and stdlib `unittest.mock.patch.object` zero-call spies confirming completely suppressed target side-effects over state mutation and `LifecycleEventSink` flushing.
- Resolved Issue #556 exactly. Issue #556 is an authorized cross-slice correction (transitions.py is owned by slice-lifecycle-engine); the owning slice work log was updated accordingly.
