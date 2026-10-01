---
title: Retrieval Content Truncation Contract
section: 05-retrieval
type: contract
status: active
tags: [section/retrieval, status/active, type/contract]
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
---
# Retrieval Content Truncation Contract

Musubi retrieval surfaces may return a bounded snippet instead of the complete stored content.
Every bounded row exposes whether it was truncated, the original character length, and the stable
`object_id` callers can use to fetch the complete object.

## Contract

- `content_truncated` is true only when the source content exceeds the applied display cap.
- `content_length` is the original Python character count, never the encoded byte length.
- Exact-cap content is not truncated.
- Ranked, recent, fast, deep, blended, and context-pack projections use the same metadata semantics.
- The metadata is backward-compatible when absent from an older serialized row.
- Cutting is grapheme-safe: every cap goes through `truncate_grapheme_safe`
  (`src/musubi/retrieve/grapheme_truncation.py`), used by the fast, recent, orchestration
  (deep/blended) and context-pack projections, so an emoji sequence, combining mark or flag is never
  split. `content_length` still counts code points.
- Parity across the external client adapters is not verified here.

## Test Contract

1. `test_production_snippet_boundary_is_truthful`
2. `test_production_snippet_length_counts_unicode_characters`
3. `test_retrieve_wire_emits_truncation_metadata`
4. `test_ranked_projection_declares_facts_after_301_1501_and_at_end_unavailable`
5. `test_unicode_cluster_at_ranked_boundary_is_never_silent`
6. `test_wire_models_keep_backward_compatible_defaults`
7. `test_context_pack_reports_its_actual_display_cap`
8. `test_context_pack_length_uses_normalized_display_text`
