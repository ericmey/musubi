---
title: "Slice: distinguish reranker degradation causes"
slice_id: slice-ret018-reranker-warning-causes-689
issue: 689
section: _slices
type: slice
status: in-review
owner: codex-gpt5-yua
phase: "3 Retrieval"
tags: [section/slices, status/in-review, type/slice, retrieval, rerank, api]
updated: 2026-08-14
reviewed: false
depends-on: [slice-ret019-tei-batch-contract-690]
blocks: []
---

# Slice: distinguish reranker degradation causes

`reranker_failed` truthfully says that retrieval fell back to fused ranking,
but it does not tell a caller whether the cause was a timeout, an unavailable
service, a rejected request, or an invalid response. The detail must become
observable without removing the stable warning old consumers already know.

## Specs to implement

- [[05-retrieval/orchestration]]
- [[07-interfaces/canonical-api]]
- [[13-decisions/0044-additive-reranker-degradation-causes]]

## Decision boundary

- Preserve `reranker_failed` on every degraded rerank response.
- Add exactly one bounded detail code for the observed cause; never expose
  exception text, status bodies, URLs, or candidate content.
- Classify timeout, request rejection, service/network unavailability,
  invalid response, and unexpected internal failure.
- Preserve `musubi_retrieval_warnings_total{warning,plane}` exactly. Record
  cause on a separate bounded counter rather than widening the existing label
  contract or double-counting its base warning.
- Make `/v1/retrieve`, `/v1/context`, retrieve streaming, MCP, and LiveKit
  preserve the same additive detail code.
- Do not change retrieval fallback, ranking, timeout budgets, or TEI batching.

## Test Contract

- `test_reranker_failure_cause_is_bounded_and_wire_additive`
- `test_reranker_failure_causes_dedupe_without_double_counting_base_warning`
- `test_tei_reranker_classifies_timeout_rejection_unavailable_and_invalid_response`
- `test_deep_stage_timeout_surfaces_timeout_cause`
- `test_retrieve_context_and_stream_share_reranker_cause_codes`
- `test_mcp_and_livekit_preserve_reranker_cause_detail`

## Work log

- 2026-08-14 — Decision/test contract opened while RET-019 was in final CI.
  The shared TEI client remains untouched until RET-019 lands and is merged
  into this branch.
- 2026-08-14 — Implemented one backward-compatible warning vocabulary:
  `reranker_failed` remains the stable public signal and exactly one bounded
  detail code identifies timeout, rejected request, unavailable service,
  invalid response, or unexpected error. Existing warning telemetry remains
  unchanged; a separate bounded cause counter records the new dimension.
- 2026-08-14 — Preserved the additive detail across retrieve, context, stream,
  MCP, and LiveKit surfaces without exposing exception text, response bodies,
  URLs, or candidate content. Timeout classification is explicit at both the
  TEI transport and deep-stage deadline boundaries.
- 2026-08-14 — Merged RET-019 after its reviewed merge to main. The one test
  conflict retained both contracts: runtime `/info` remains authoritative for
  TEI batch ceilings, while typed TEI failures now supply RET-018 causes.
  Combined focused verification passed 59 tests with 16 intentional skips.
- 2026-08-14 — Final verification on the merged ancestry: `make check` passed
  with 2,685 tests, 195 skips, 140 deselections, and 2 expected xfails;
  formatting, lint, strict mypy, and coverage all passed. `make tc-coverage`
  closed 78/78 stated bullets, including all six slice bullets, and
  `make agent-check` completed clean with warnings only.
- 2026-08-14 — The three pre-existing live integration/performance bullets in
  the orchestration spec (10K fast-path p95, deep-path NDCG, and kill-TEI
  degradation) remain outside this API-contract slice; their existing
  retrieval-eval and live-stack homes are unchanged.
- 2026-08-14 — Independent exact-head review found a latent runtime seam: an
  out-of-vocabulary cause from a future Embedder would be rejected before the
  wire helper could preserve `reranker_failed`. The warning factory now
  normalizes every unknown runtime cause to bounded `unexpected_error`; a
  regression proves both the stable base and bounded detail survive.
