---
owner: gemini-3-1-shiori
status: in-review
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
- `test_truncation_bypasses_short_text`
- `test_truncation_cuts_at_grapheme_boundaries_safely`
- `test_truncation_respects_max_chars_lte_3`
- `test_truncation_prevents_emoji_zwj_bisection`
- `test_truncation_preserves_single_emoji`
- `test_truncation_prevents_combined_diacritic_bisection`
- `test_truncation_prevents_regional_indicator_bisection`
- `test_truncation_preserves_internal_whitespace`
- `test_truncation_preserves_trailing_whitespace_if_within_budget`
- `test_truncation_prevents_skin_tone_modifier_bisection`
- `test_fast_retrieval_uses_grapheme_truncation_for_long_content`
- `test_recent_retrieval_uses_grapheme_truncation_for_long_content`
- `test_orchestration_uses_grapheme_truncation_for_long_content`
- `test_context_pack_uses_grapheme_truncation_for_long_content`

## Definition of Done
- Grapheme-safe truncation implemented.
- `make check` is fully passing.

## Work log
- Base orchestration integration details (fast/deep skips and timeouts, bullets 1-12) are pre-existing out-of-scope conditions tracked elsewhere.
- Base orchestration integration details (fast/deep skips and timeouts, bullets 1-12) are pre-existing out-of-scope conditions tracked elsewhere.
