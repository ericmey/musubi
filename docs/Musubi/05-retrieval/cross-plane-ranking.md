---
title: Cross-Plane Ranking Globally Comparable
section: 05-retrieval
type: contract
status: active
tags: [section/retrieval, status/active, type/contract]
updated: 2026-07-15
up: "[[05-retrieval/index]]"
reviewed: false
---
# Cross-Plane Ranking Globally Comparable

When a retrieval request fans out to more than one `(namespace, plane)` target, the merged candidate list must use a single comparable relevance calibration derived from the full candidate set, not per-target local batch maxima.

A weak plane's sole hit must not become maximally relevant merely by being alone. The cross-encoder rerank path keeps its intrinsic `sigmoid(rerank_score)` relevance. The final merge order is deterministic: higher score first, then lower `object_id`, then lower `plane`.

## Contract

- The global calibration is `max(raw_rrf_score)` across the full pre-dedup fanout candidate set. No corpus-level percentile, no stored per-plane p99.
- When a leg carries a `rerank_score` (deep / blended mode), the leg's relevance is `_sigmoid(rerank_score)` — the same intrinsic function `scoring._relevance` already uses.
- When a leg carries only a `raw_rrf_score` (fast mode), the leg's relevance is `_clamp01(raw_rrf_score / global_max)`.
- When a leg carries neither (recent mode), the leg's `score` and `score_components` are passed through unchanged.
- The seam runs over the full pre-dedup candidate list, then the existing `best_by_id` dedup picks the highest-recalibrated copy per `object_id`. Calibrating after dedup can permanently discard the better copy using the bad per-leg score.
- The final sort key is `(-score, object_id, plane)`. The current multi-target sort has no secondary key.
- The seam is gated on the multi-target branch only. The `len(targets) == 1` branch is bit-for-bit unchanged.
- The two optional raw fields (`raw_rrf_score`, `raw_rerank_score`) live on the internal `RetrievalResult` only. They are not projected onto the wire models (`RankedResultRow`, `RecentResultRow`, `ContextPackItem`).

## Test Contract

1. `test_asymmetric_two_plane_fast_weak_sole_does_not_maximize`
2. `test_three_plane_wildcard_uses_global_calibration`
3. `test_pre_dedup_calibration_picks_higher_recalibrated_copy`
4. `test_cross_plane_tiebreak_object_id_then_plane`
5. `test_dedup_equal_score_prefers_lower_plane` (parametrized over gather order)
6. `test_single_target_fast_path_unchanged`
7. `test_rerank_sigmoid_relevance_unchanged`
8. `test_recent_mode_passthrough_at_seam`
9. `test_empty_working_set_no_op`
