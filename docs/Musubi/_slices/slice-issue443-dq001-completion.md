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
  - status/in-review
  - type/slice
updated: 2026-07-15
reviewed: true
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
- `docs/Musubi/13-decisions/0037-grapheme-safe-truncation-dependency.md`
- `src/musubi/retrieve/context_pack.py`
- `src/musubi/retrieve/fast.py`
- `src/musubi/retrieve/grapheme_truncation.py`
- `src/musubi/retrieve/orchestration.py`
- `src/musubi/retrieve/recent.py`
- `tests/retrieve/test_dq001_truncation.py`
- `pyproject.toml`
- `uv.lock`

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

### Out-of-scope: pre-existing orchestration contract bullets

This slice cites `[[05-retrieval/orchestration]]` only for its grapheme-safe
projection bullets. The spec's earlier orchestration gaps are pre-existing and
tracked by Issue #509; they are not implemented or claimed here:

- `test_fast_mode_skips_rerank` — pre-existing, out-of-scope; follow-up #509.
- `test_deep_mode_invokes_rerank` — pre-existing, out-of-scope; follow-up #509.
- `test_fast_mode_skips_lineage_hydrate` — pre-existing, out-of-scope; follow-up #509.
- `test_deep_mode_hydrates_when_flag_true` — pre-existing, out-of-scope; follow-up #509.
- `test_steps_run_in_documented_order` — pre-existing, out-of-scope; follow-up #509.
- `test_planes_run_in_parallel` — pre-existing, out-of-scope; follow-up #509.
- `test_hydrate_fetches_run_in_parallel` — pre-existing, out-of-scope; follow-up #509.
- `test_whole_call_timeout_fast_400ms` — pre-existing, out-of-scope; follow-up #509.
- `test_per_plane_timeout_deep_1500ms` — pre-existing, out-of-scope; follow-up #509.
- `test_rerank_timeout_returns_with_warning` — pre-existing, out-of-scope; follow-up #509.
- `test_deterministic_for_fixed_inputs` — pre-existing, out-of-scope; follow-up #509.
- `test_tiebreak_on_object_id` — pre-existing, out-of-scope; follow-up #509.
- `integration: end-to-end fast-path on 10K corpus with real TEI + Qdrant, p95 ≤ 400ms` — integration, out-of-scope; follow-up #509.
- `integration: end-to-end deep-path with rerank, NDCG@10 on golden set ≥ threshold` — integration, out-of-scope; follow-up #509.
- `integration: kill TEI mid-request, pipeline returns with documented degradation` — integration, out-of-scope; follow-up #509.
