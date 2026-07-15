---
title: "Slice: LIFE-011 — Promotion Saga Invariants"
slice_id: slice-life011-promotion-saga
status: in-review
owner: gemini-3-1-shiori
phase: "Lifecycle"
section: _slices
type: slice
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---

# Slice: LIFE-011 — Promotion Saga Invariants

Tracks #555.

## What
Closes the remaining write-before-create recovery and post-commit notification classification gaps. Restructures the promotion path to guarantee deterministic data persistence sequence, ensuring `LIFE-004` safely resolves partial failures while `LIFE-005` natively maintains the established identity-reuse guarantees.

## Specs to implement
- [[06-ingestion/promotion]]

## Files
- `owns_paths`: 
  - `src/musubi/lifecycle/promotion.py`
  - `tests/lifecycle/test_life011_promotion_saga.py`
  - `docs/Musubi/_slices/slice-life011-promotion-saga.md`

## Test Contract
1. `test_life011_saga_recovers_curated_create_failure`
2. `test_life011_saga_recovers_concept_transition_failure`
3. `test_life011_saga_absorbs_thought_emit_failure_without_rejection`

## Work log
- Reordered `write_curated` natively before the Qdrant `curated_plane.create` mapping enabling `LIFE-005` to safely capture missing indices on replay.
- Idempotently updates the pre-written vault frontmatter explicitly passing the accepted identity backwards mapping Qdrant verification boundaries correctly.
- Switched downstream `thoughts.emit` into a self-contained `try/except` capturing `log.warning` specifically, preventing late-bound observability omissions from improperly rejecting fully processed promotions and creating permanent anomalies.
