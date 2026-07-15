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
Closes the remaining promotion recovery and post-commit notification classification gaps. Preserves the existing create-first identity validation, proves that a failed vault write re-adopts the one persisted row on retry, and prevents notification failure from reclassifying committed promotion state.

## Specs to implement
- [[06-ingestion/promotion]]

## Files
- `owns_paths`:
  - `src/musubi/lifecycle/promotion.py`
  - `tests/lifecycle/test_life011_promotion_saga.py`
  - `docs/Musubi/_slices/slice-life011-promotion-saga.md`

## Test Contract
1. `test_life011_saga_recovers_vault_write_failure`
2. `test_life011_saga_recovers_concept_transition_failure`
3. `test_life011_saga_absorbs_thought_emit_failure_without_rejection`

## Work log
- Preserves create-first identity validation so unrelated-lineage conflicts fail before any vault overwrite.
- Proves a retry after vault-write failure re-adopts the existing curated row and writes that canonical identity to the file and concept transition.
- Switched downstream `thoughts.emit` into a self-contained `try/except` capturing `log.warning` specifically, preventing late-bound observability omissions from improperly rejecting fully processed promotions and creating permanent anomalies.
