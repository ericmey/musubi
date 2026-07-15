---
owner: gemini-3-1-shiori
status: in-progress
issue: 534
title: "Slice: ING-001 semantic dedup strict compatibility bounds"
slice_id: slice-issue534-ing001-semantic-dedup
section: _slices
type: slice
phase: "Ingestion"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: ING-001 semantic dedup strict compatibility bounds

## Context
Semantic dedup may merge only factually compatible duplicates. Corrections, negations, participant or time changes, conflicting numbers, and ambiguous near-matches must remain distinct and preserve lineage.

## Specs to implement
- [[06-ingestion/capture]] (assuming deduplication rule is documented here)

## Owned paths
- `src/musubi/ingestion/capture.py`
- `tests/ingestion/test_capture.py`

## Forbidden paths
- Qdrant cluster indexing logic, LLM adapter internal implementations. No new engine.

## Test Contract
- Pending definition...

## Definition of Done
- Strict factual compatibility evaluated during dedup.
- `make check` is fully passing.

## Work log
