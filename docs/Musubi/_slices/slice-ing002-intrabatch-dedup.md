---
title: "Slice: ING-002 — Intra-batch dedup equivalence"
slice_id: slice-ing002-intrabatch-dedup
status: in-review
owner: shiori@home
phase: "Ingestion"
section: _slices
type: slice
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-16
reviewed: false
depends-on: []
blocks: []
---

# Slice: ING-002 — Intra-batch dedup equivalence

Tracks #533.

## What

Prove batch ingestion is equivalent to sequential single-create behavior for duplicate/near-duplicate ordering, corrections/negations (preserving ING-001 semantic protections), mixed namespaces/planes, and deterministic returned identities.

Fix the `batch_create` shared seam to deduplicate within the current in-flight batch rather than independently querying Qdrant against stale pre-batch state for each row.

## Specs to implement
- [[06-ingestion/capture]]

## Files
- `owns_paths`: 
  - `src/musubi/planes/episodic/plane.py`
  - `tests/planes/test_episodic.py`
  - `docs/Musubi/_slices/slice-ing002-intrabatch-dedup.md`

## Test Contract
1. `test_batch_create_intra_batch_rejects_factual_incompatibility`
2. `test_batch_create_cross_namespace_isolation`
3. `test_intrabatch_dedup_sequential_duplicate`
4. `test_intrabatch_dedup_prefers_best_score_and_tie_breaks`
5. `test_batch_create_enforces_100_item_limit`
6. `test_batch_vs_sequential_multiple_clusters`
7. `test_batch_vs_sequential_permuted_order`
8. `test_intrabatch_dedup_sequential_tiebreak_equal_score`

## Work log
