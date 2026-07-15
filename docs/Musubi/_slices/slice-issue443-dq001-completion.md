---
owner: gemini-3-1-shiori
status: in-progress
issue: 443
title: "Slice: DQ-001 complete grapheme-safe and adapter parity"
slice_id: slice-issue443-dq001-completion
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
# Slice: DQ-001 complete grapheme-safe and adapter parity

## Context
Complete DQ-001 requirements (Issue #443). Replace codepoint slicing in fast/recent/ranked/context with one shared grapheme-safe projection seam. Preserve content_truncated/content_length/object_id recovery handle.

## Specs to implement
- [[05-retrieval/orchestration]]
- Issue #443

## Owned paths
- `src/musubi/retrieve/orchestration.py`
- `src/musubi/retrieve/context_pack.py`
- `tests/retrieve/test_dq001_truncation.py`

## Forbidden paths
- Renaming `content` to `snippet` in API responses.

## Test Contract
- Pending

## Definition of Done
- Grapheme-safe truncation implemented.
- `make check` is fully passing.

## Work log
