---
title: Deep Path
section: 05-retrieval
tags: [deep, planning, retrieval, section/retrieval, slow-thinker, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/deep.py", "tests/retrieve/test_deep.py"]
---
# Deep Path

Retrieval for planning, analysis, and background pre-fetch. Uses the full pipeline including reranker + lineage hydration. Budget is loose (p95 ≤ 5 s) because the caller isn't a human waiting on a keystroke.

## Typical callers

- **Voice adapters** that pre-fetch context while the user is still speaking (see below).
- **Coding-agent planning loops**: ahead-of-action retrieval to ground the plan in memory.
- **Blended mode**: `run_blended_retrieve` runs one deep retrieval per plane ([[05-retrieval/blended]]).
- **Evals harness**: replays corpus queries against a fixture (see [[05-retrieval/evals]]).

## Invocation

```python
results = await musubi.retrieve(
    RetrievalQuery(
        namespace="alex/claude-code/curated",
        query_text="how did we decide to promote concepts",
        mode="deep",
        limit=25,
        planes=["curated"],
        include_lineage=True,
    )
)
```

`include_lineage=True` is the default. It enables lineage hydration after scoring.

## Pipeline

`run_deep_retrieve` (`src/musubi/retrieve/deep.py`):

1. **Optional LLM query expansion.** If a `DeepRetrievalLLM` is passed, its `expand_query` runs under a 2 s deadline and its output is appended to the query text; any failure falls back to the raw query. The HTTP router does not pass one, so over `/v1/retrieve` this step is a no-op.
2. **Hybrid search per plane** with `limit = query.limit * 2` (headroom for the reranker). Per-plane budget 1.5 s; sparse encoding gets 1.0 s, after which that leg continues dense-only with a `sparse_embedding_failed` warning.
3. **Merge** per-plane hits by `object_id` (highest fused score wins).
4. **Cross-encoder rerank** of the merged candidates against the original query text, keeping `top_k = query.limit`, under `retrieval_rerank_timeout_s` (default 1.5 s). Five or fewer candidates skip the reranker. See [[05-retrieval/reranker]].
5. **Score** with the unified scorer; relevance becomes the sigmoid of the rerank score ([[05-retrieval/scoring-model]]).
6. **Lineage hydration** per hit, concurrently, each under `retrieval_lineage_timeout_s` (default 0.5 s).

## What deep path adds over fast path

1. **Cross-encoder rerank.** BGE-reranker-v2-m3 scores each candidate against the query. Replaces the RRF-based `relevance` input.
2. **Lineage hydration.** Fetches:
   - The full stored content and title of each hit.
   - Supersession chain tips (so the caller can follow "what replaced this?").
   - Source artifact metadata for any `supported_by` references.
   - Promoted-from / promoted-to for concepts and curated.

   Hydration reads use `bump_access=False`: a lineage hop is never counted as a delivered row (RET-002; see [[05-retrieval/orchestration]]).
3. **Larger budgets** — more prefetch, looser timeouts.

## Pre-fetch pattern for voice adapters

A voice adapter can run two loops in parallel: a fast loop that uses the fast path for anything it must say right now, and a slower loop that runs deep retrieval on the accumulating transcript and holds the results for the next turn. When the fast loop needs context, it checks the pre-fetched results first and falls back to the fast path if they are not ready.

That cache, if any, belongs to the adapter (for example `sourceblender/musubi-livekit`), not to Musubi Core. See [[07-interfaces/livekit-adapter]].

## Result shape additions

Deep-path results include hydrated lineage:

```json
{
  "object_id": "...",
  "plane": "curated",
  "title": "Release checklist v3",
  "snippet": "...",
  "score": 0.82,
  "score_components": { ... },
  "lineage": {
    "supersedes": [
      {"object_id": "...", "title": "Release checklist v2", "state": "superseded"}
    ],
    "superseded_by": null,
    "promoted_from": {"object_id": "...", "title": "Release checklist pattern"},
    "supported_by": [
      {"artifact_id": "...", "chunk_id": "...", "title": "release-notes.pdf"}
    ]
  },
  "payload": { ... full body or large snippet ... }
}
```

Fast-path results have a `lineage` field too, built from the payload only (IDs, no hydrated titles/bodies).

## Caching at this tier

Deep-path results are **not** response-cached. The queries are varied, the corpus changes, and a stale deep-path result is worse than a fresh one.

## LLM-in-the-loop

Musubi stops at returning ranked passages; the caller does the LLM work. The only LLM touchpoint in deep retrieval is the optional `DeepRetrievalLLM.expand_query` hook above. A fuller RAG mode (filtering for factuality, summarising across the top N) is **planned, not implemented**.

## Failure handling (deep)

Softer than fast path — deep path callers generally can retry or degrade:

| Failure | Response |
|---|---|
| Rerank error or timeout | Fall back to the fused (RRF) order + `reranker_failed` warning with a bounded `cause`. |
| Lineage hydrate failure or timeout | Return that hit unhydrated + a log line with the object id. |
| Sparse encoding slow (> 1.0 s) | Continue dense-only + `sparse_embedding_failed` warning. |
| One plane's hybrid query fails | `run_deep_retrieve` returns that error; the orchestrator turns a per-target timeout into a `plane_timeout_<plane>` warning when other targets survive. |
| Whole target exceeds 5 s | `kind="timeout"` (503 if no target survives). |

Operationally, rerank and lineage are bounded optional stages. The defaults are
`retrieval_rerank_timeout_s=1.5` and
`retrieval_lineage_timeout_s=0.5`; both are validated positive settings. A
rerank timeout returns the pre-rerank hybrid order with the structured
`reranker_failed` warning. A lineage timeout returns the unhydrated hit and logs
the object id plus budget. Qdrant-backed hybrid queries, authoritative
resolution, and deep-path lineage reads run outside the event-loop thread so
concurrent blended callers do not starve one another before the five-second
whole-call deadline. These hybrid/deep operations use two dedicated executors
capped at 16 total active calls per API process: eight slots reserved for
required query and authoritative-resolution work, and eight isolated slots for
optional lineage hydration. Excess work queues behind its stage ceiling instead of consuming the
asyncio default executor, with the submitting request and trace context copied
into each worker call. The regression suite (`tests/retrieve/test_ret016_bounded_offload.py`) covers 20 concurrent
callers through the public deep path, including query, authoritative resolution,
rerank, scoring, and lineage stages. When the optional executor is saturated,
the per-hit lineage deadline still returns the original unhydrated hit without
starving a required query or failing the whole request.

Lineage hydration has no per-hit event-loop adapter. Its worker-thread seam may
only call plane reads that complete synchronously without suspending; if a plane
read later awaits a loop-bound resource, hydration fails loudly and must gain a
genuine synchronous read seam before use here.

The separate recent/context retrieval path is outside this executor contract.

The 1.5 s rerank default is derived from a measurement on the reference
deployment rather than inherited from the earlier 800 ms spec: a ten-caller
burst measured reranker duration at approximately p50 0.684 s, p95 1.226 s, and
p99 1.268 s across 200 candidate predictions. The 1.5 s default clears that
loaded p99 while leaving the lineage stage and whole-call deadline bounded.

## Observability

Deep path shares the retrieval counters: `musubi_retrieval_warnings_total{warning, plane}` (e.g. `reranker_failed`, `sparse_embedding_failed`), `musubi_reranker_degradation_causes_total{cause, plane}` and `musubi_retrieval_errors_total{kind}` (`src/musubi/observability/retrieval_metrics.py`). There are no deep-specific latency histograms.

## Test Contract

**Module under test:** `src/musubi/retrieve/deep.py`

Happy path:

1. `test_deep_path_invokes_rerank`
2. `test_deep_path_hydrates_lineage_by_default`
3. `test_deep_path_snippet_longer_than_fast`
4. `test_deep_path_p95_under_5s_on_100k_corpus` — deferred (skipped stub)

Concurrency and caching:

5. `test_deep_path_parallel_safe_under_concurrent_callers` — deferred (skipped stub; concurrent callers are exercised by `test_twenty_callers_complete_through_the_production_deep_path`)
6. `test_deep_path_no_response_cache_by_default` — deferred (skipped stub)

Degradation:

7. `test_deep_path_rerank_down_falls_back_with_warning`
8. `test_deep_path_hydrate_missing_artifact_partial_lineage`
9. `test_deep_path_one_plane_timeout_degrades` — deferred (skipped stub)
10. `test_run_deep_retrieve_honors_caller_stage_budgets`

Property and integration (deferred: skipped stubs):

11. `test_hypothesis_deep_path_result_ordering_is_stable_for_fixed_inputs_and_weights` — deferred
12. `test_integration_livekit_slow_thinker_scenario` — deferred
13. `test_integration_deep_path_vs_fast_path_on_the_same_query` — deferred
