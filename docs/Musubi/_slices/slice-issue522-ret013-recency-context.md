---
owner: gemini-3-1-shiori
status: in-review
issue: 522
title: "Slice: RET-013 bounded recent-memory lane in canonical cross-modality context"
slice_id: slice-issue522-ret013-recency-context
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
# Slice: RET-013 bounded recent-memory lane in canonical cross-modality context

## Context
Implement a bounded recent-memory lane inside `/v1/context` (Issue #522). Canonical agent recall blends recent (including provisional immediately) with the highest-ranked established memories. Recent must be capped and deduped against ranked results, with provenance, state, warnings, and truncation surviving. Cross-modality federation uses explicitly authorized concrete namespace targets (without undoing RET-011 exact filtering).

## Specs to implement
- [[05-retrieval/context-pack]]

## Owned paths
- `src/musubi/api/routers/context.py`
- `src/musubi/retrieve/context_pack.py`
- `tests/api/test_context.py`
- `tests/retrieve/test_context_pack.py`
- `tests/api/test_ret007_telemetry_boundary.py` (RET-013 compatibility assertion only)
- `docs/Musubi/05-retrieval/context-pack.md` (RET-013 contract update only)

## Forbidden paths
- Scattered authorization exceptions (Issue #523 is out of scope).

## Test Contract
- `test_context_endpoint_blends_recent_provisional_with_established_ranked`
- `test_context_endpoint_max_chars_mix_quota`
- `test_context_endpoint_single_lane_empty_cases`
- `test_context_endpoint_custom_state_filter_applies_to_both_lanes`

## Definition of Done
- Mixed lane implemented.
- `make check` is fully passing.

## Work log
- 2026-07-15: Independent Yua closeout integrated RET-012/main once without
  conflict, repaired the isolated worktree from the locked dependency set, and
  reran the combined gates: `make check` passed with 2,136 tests, 194 skips,
  four expected failures, and coverage above the repository threshold. The
  context-pack Test Contract is now mechanically numbered; `tc-coverage`
  proves all 15 bullets passing, including all four RET-013 bullets.
- 2026-07-15: RET-013 now invokes the shared orchestration boundary once for the
  recent lane and once for the ranked lane. Updated the existing RET-007 context
  telemetry assertion to require one warning-counter increment per degraded lane
  (two total), while the response continues to deduplicate the warning code.
