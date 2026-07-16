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
1. `test_batch_create_intra_batch_exact_duplicate`
2. `test_batch_create_intra_batch_normalized_duplicate`
3. `test_batch_create_intra_batch_rejects_factual_incompatibility`
4. `test_batch_create_cross_namespace_isolation`

## Work log
