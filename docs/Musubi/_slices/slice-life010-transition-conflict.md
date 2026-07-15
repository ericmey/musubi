---
title: "Slice: LIFE-010 — Transition Conflict Hard Fence"
slice_id: slice-life010-transition-conflict
status: in-review
owner: shiori@home
phase: "Lifecycle"
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

## Files
- `owns_paths`: 
  - `src/musubi/lifecycle/transitions.py`
  - `tests/lifecycle/test_lifecycle.py`

## Test Contract
1. `test_concurrent_transitions_stale_expected_version_fence_violation`

## Work Log
- Implemented `version_fence_violation` strict enforcement in `transitions.py`.
- Rebuilt discriminator asserting strict `Err` format and `mocker.spy` confirming completely suppressed target side-effects over state mutation and `LifecycleEventSink` flushing.
- Resolved Issue #556 exactly.
