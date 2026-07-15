---
owner: gemini-3-1-shiori
status: in-progress
issue: 522
title: "Slice: RET-013 bounded recent-memory lane in canonical cross-modality context"
slice_id: slice-issue522-ret013-recency-context
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: RET-013 bounded recent-memory lane in canonical cross-modality context

## Context
Implement a bounded recent-memory lane inside `/v1/context` (Issue #522). Canonical agent recall blends recent (including provisional immediately) with the highest-ranked established memories. Recent must be capped and deduped against ranked results, with provenance, state, warnings, and truncation surviving. Cross-modality federation uses explicitly authorized concrete namespace targets (without undoing RET-011 exact filtering). 

## Specs to implement
- Issue #522 (Wait, let me verify actual specs for this)

## Owned paths
- `src/musubi/api/routers/context.py`
- `tests/api/test_context.py`

## Forbidden paths
- Scattered authorization exceptions (Issue #523 is out of scope).

## Test Contract
- Pending definition...

## Definition of Done
- Mixed lane implemented.
- `make check` is fully passing.

## Work log
