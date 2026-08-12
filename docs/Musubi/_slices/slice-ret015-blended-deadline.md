---
title: "Slice: keep blended lineage retrieval within its concurrency deadline"
slice_id: slice-ret015-blended-deadline
issue: 679
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, retrieval, concurrency]
updated: 2026-08-12
reviewed: false
depends-on: [slice-data001-phase2-immutable-vectors, slice-ret007-degradation]
blocks: []
---

# Slice: keep blended lineage retrieval within its concurrency deadline

Repair the production `503 BACKEND_UNAVAILABLE` cliff observed when concurrent
Hermes callers use the canonical blended request shape: three default planes,
`limit=5`, provisional visibility, and lineage hydration.

## Production evidence

- Sequential control before and after the burst: `200` in 1.023 s and 0.949 s.
- Ten concurrent default requests: 7 `200`, 3 `503` at 5.067 s.
- Same burst with `include_lineage=false`: 10/10 `200`.
- Episodic-only with lineage: 10/10 `200`.
- Episodic-only without lineage: 10/10 `200`.
- TEI embedding averaged 14-35 ms; reranking averaged 675 ms during the probe.
- The async retrieval path calls synchronous Qdrant authoritative-anchor and
  lineage resolution reads on the event-loop thread. The 5 s whole-call timeout
  cancels otherwise healthy work once those reads serialize under contention.

## Decision boundary

- Preserve the public retrieve request and response schema.
- Preserve authoritative-anchor resolution, lifecycle visibility, lineage
  semantics, and final-result access accounting.
- Move blocking Qdrant resolution work off the event-loop thread.
- Enforce the already-specified rerank and lineage stage budgets as graceful
  degradation before the existing whole-call deadline.
- Do not raise the 5 s deadline as the repair; that only moves the cliff.
- Client fallback is a separate fleet-tools change: one blended attempt, then
  one fast attempt on the first 503, never a second blended retry.

## Owned paths

- `src/musubi/retrieve/hybrid.py`
- `src/musubi/retrieve/deep.py`
- `tests/retrieve/test_ret015_blended_deadline.py`
- `docs/Musubi/05-retrieval/deep-path.md` (deadline/degradation contract only)
- `docs/Musubi/05-retrieval/orchestration.md` (deadline/degradation contract only)
- `docs/Musubi/_slices/slice-ret015-blended-deadline.md`
- `docs/Musubi/_inbox/locks/slice-ret015-blended-deadline.lock`

## Forbidden paths

- `src/musubi/api/`
- `src/musubi/planes/`
- `src/musubi/types/`
- Deployment manifests and live-host configuration

## Test Contract

- `test_anchor_resolution_does_not_block_the_event_loop`
- `test_lineage_hydration_does_not_block_the_event_loop`
- `test_reranker_timeout_degrades_to_hybrid_with_warning`
- `test_lineage_timeout_returns_unhydrated_hit_instead_of_failing_the_request`
- `test_default_blended_shape_finishes_concurrently_before_the_whole_call_deadline`
- `test_retrieval_semantics_are_preserved_after_async_offload`

## Work log

- 2026-08-12 — `codex-gpt5` reproduced the production failure on v1.23.4,
  isolated the interaction to three-plane fanout plus lineage, and verified the
  blocking Qdrant reads directly in the deployed source. Claimed Issue #679 and
  opened draft PR #680 before code changes.

