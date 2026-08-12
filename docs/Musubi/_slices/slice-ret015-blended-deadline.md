---
title: "Slice: keep blended lineage retrieval within its concurrency deadline"
slice_id: slice-ret015-blended-deadline
issue: 679
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-review, type/slice, retrieval, concurrency]
updated: 2026-08-12
reviewed: false
depends-on: [slice-data001-phase2-immutable-vectors, slice-ret007-degradation]
blocks: []
---

# Slice: keep blended lineage retrieval within its concurrency deadline

Repair the production `503 BACKEND_UNAVAILABLE` cliff observed when concurrent
Hermes callers use the canonical blended request shape: three default planes,
`limit=5`, provisional visibility, and lineage hydration.

## Specs to implement

- [[05-retrieval/deep-path]]
- [[05-retrieval/orchestration]]

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
- Preserve existing whole-request and TEI metrics. This slice adds bounded
  degradation logs at the newly enforced stage boundaries; a new metrics
  vocabulary is outside the public-behaviour repair.

## Owned paths

- `src/musubi/retrieve/hybrid.py`
- `src/musubi/retrieve/deep.py`
- `src/musubi/settings.py` (retrieval stage budgets only)
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
- `test_retrieval_stage_budgets_are_tunable_positive_settings`

## Work log

- 2026-08-12 — `codex-gpt5` reproduced the production failure on v1.23.4,
  isolated the interaction to three-plane fanout plus lineage, and verified the
  blocking Qdrant reads directly in the deployed source. Claimed Issue #679 and
  opened draft PR #680 before code changes.
- 2026-08-12 — Tests-first repair offloads authoritative-anchor resolution and
  lineage hydration from the event-loop thread, enforces configurable 1.5 s
  rerank and 500 ms per-hit lineage budgets, and degrades to hybrid/unhydrated
  results before the unchanged five-second whole-call deadline. Focused retrieval
  suite: 93 passed, 16 documented skips. Whole-repo `make check`: 2,639 passed,
  195 skipped, 140 deselected, two expected xfails, 90% total coverage.
- 2026-08-12 — Test Contract closure boundaries for inherited specs: the
  pre-existing `test_reflection_prompts_resolved_via_deep_path` and
  `test_reflection_results_include_provenance_for_audit` bullets remain outside
  RET-015; they describe lifecycle reflection integration and belong to a
  reflection/evals follow-up, not this concurrency repair. The inherited
  property/integration bullets are also out of scope here: `hypothesis: deep
  path result ordering is stable for fixed inputs and weights`, `integration:
  LiveKit Slow Thinker scenario — pre-fetched context available within 2s while
  user is speaking`, `integration: deep path vs fast path on the same query —
  deep NDCG@10 higher by ≥ 5 points on evals corpus`, `integration: end-to-end
  fast-path on 10K corpus with real TEI + Qdrant, p95 ≤ 400ms`, `integration:
  end-to-end deep-path with rerank, NDCG@10 on golden set ≥ threshold`, and
  `integration: kill TEI mid-request, pipeline returns with documented
  degradation`. Their existing follow-up homes are the retrieval evals,
  LiveKit, and ops-GPU slices named by the adjacent skips; RET-015 adds the
  production-derived compressed concurrency proof and requires a live
  post-deploy replay of the exact Hermes burst instead.
- 2026-08-12 — Aoi's review challenged the inherited 800 ms rerank budget and
  the unbounded default-executor/per-hit-event-loop seams. A live ten-caller
  burst measured reranker duration across 200 candidate predictions at p50
  0.684 s, p95 1.226 s, and p99 1.268 s; queue p95 was 1.181 s while inference
  p95 was 0.345 s. The default is therefore 1.5 s, clearing the loaded p99 with
  measured headroom. Default-executor saturation beyond ten callers and the
  per-hit `asyncio.run()` adapter are explicit follow-up Issue #681 rather than
  unclaimed residual risk.
- 2026-08-12 — Handoff at `f6614d1` plus the final documentation commits:
  whole-repo `make check` passed with 2,640 tests, 195 documented skips, 140
  deselected, two expected xfails, and 90% coverage. `make tc-coverage` and
  `make agent-check` are green (warnings only). Draft PR #680 is ready for the
  required second-agent review; live deployment remains untouched.
