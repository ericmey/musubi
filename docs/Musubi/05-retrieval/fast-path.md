---
title: Fast Path
section: 05-retrieval
tags: [fast-path, latency, retrieval, section/retrieval, status/complete, type/spec, voice]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/fast.py", "tests/retrieve/test_fast.py"]
---
# Fast Path

Retrieval for voice, chat, and any surface where a human is actively waiting. The colocated-inference target is p95 ≤ 400 ms end-to-end, including the adapter round trip, with TEI and Qdrant on the same host as core. A deployment with remote inference must measure its own latency before choosing larger deadlines; raising them does not satisfy the colocated target.

## The plan

```
adapter.query()
  │ 1. validate + authorize (router)
  │ 2. encode query (dense + sparse)      bounded by retrieval_fast_encoding_timeout_s
  │ 3. hybrid search per plane            bounded by retrieval_fast_plane_timeout_s (each)
  │ 4. dedup by object_id + score
  │ 5. pack top-K with brief snippets
  └─► response                            whole call bounded by retrieval_fast_whole_timeout_s
```

Five steps, no reranker, no LLM, no lineage hydration. Encoding, each plane search and the whole call each have a deadline. Per-step figures are not published as guarantees; measure on your own host.

## Per-step details

### Step 1: Validate + authorize

- The router parses the body (`RetrieveQuery`, `src/musubi/api/routers/retrieve.py`) and checks the token's scope against the resolved namespace targets before calling the orchestrator.
- There is no separate fast-only endpoint: `/v1/retrieve` with `mode="fast"` (the default) selects this path.

Failure: 400/422 for a malformed body, 403 for a namespace outside the token's scope.

### Step 2: Query encoding

- Each `run_fast_retrieve` call creates its own `QueryEmbeddingCache` (`src/musubi/retrieve/fast.py`). The cache is shared by the plane searches inside that call, so the query is not re-encoded per collection, but it does **not** persist across requests: a repeat query re-encodes.
- Dense and sparse encodings are requested from TEI concurrently.
- The encoding deadline is `retrieval_fast_encoding_timeout_s` (default 0.25 s). If it expires, the call fails with `embeddings_unavailable` (503).

### Step 3: Hybrid search per plane

For each plane in the call (default planes: `[curated, concept, episodic]`):

- Submit a hybrid query to Qdrant with namespace filter + state filter.
- `limit = K_pre` where `K_pre = max(20, query.limit * 2)` so there is headroom after dedup.
- Each plane search is bounded by `retrieval_fast_plane_timeout_s` (default 0.25 s).

The plane searches run concurrently under a plain `asyncio.gather`; each search catches its own timeout and turns it into a per-plane result, so a slow plane does not block the others.

Three independent settings bound encoding, each plane's search, and the whole call: `retrieval_fast_encoding_timeout_s`, `retrieval_fast_plane_timeout_s` and `retrieval_fast_whole_timeout_s` (`src/musubi/settings.py`; environment `RETRIEVAL_FAST_ENCODING_TIMEOUT_S`, `RETRIEVAL_FAST_PLANE_TIMEOUT_S`, `RETRIEVAL_FAST_WHOLE_TIMEOUT_S`). Defaults are 0.250, 0.250 and 0.400 seconds. An operator may override them after measuring the inference path. The whole-call timer includes encoding and all plane work; it can still cancel work that fits each individual stage deadline.

**"Encode once" is per target, not per request.** The orchestrator splits a request into one target per `(namespace, plane)` and runs `run_fast_retrieve` once per target concurrently (`src/musubi/retrieve/orchestration.py`). Each target encodes the query itself and has its own whole-call deadline. A request across three planes therefore encodes the query up to three times, concurrently.

### Step 4: Dedup + score

- **Dedup by `object_id` only**: if the same object surfaces from more than one plane, the copy with the higher fused score wins. Fast mode does no content-similarity dedup and no lineage-aware drop; those happen only in [[05-retrieval/blended]].
- **Score** each hit with [[05-retrieval/scoring-model]]. Relevance is the RRF score divided by the batch maximum.

When the orchestrator fans out across several targets, it merges the target results by `object_id` again after the cross-plane calibration seam ([[05-retrieval/cross-plane-ranking]]).

### Step 5: Pack response

- Sort by `(-score, object_id)`.
- Take the top `query.limit`.
- Generate `snippet`: up to 200 characters of content (or the title when content is empty), cut on a grapheme boundary. `content_truncated` and `content_length` report the cut (see [[05-retrieval/content-truncation]]).
- Compute `lineage_summary` from the payload (no extra reads).

## What fast path doesn't do

- **No cross-encoder rerank.** It would not fit the budget.
- **No LLM rewriting of queries.** The raw user text is the query.
- **No lineage hydration.** The `lineage_summary` comes from the payload; superseded objects, chunk contents and citation targets are not fetched.
- **No cross-request caching** of embeddings or responses in the API path.

## Response cache (not wired)

`src/musubi/retrieve/fast.py` defines `FastResponseCache`, a 30-second TTL cache keyed on `(namespace, query, collections, limit, state_filter)`, and `run_fast_retrieve` accepts one. The API does not pass one, and there is no setting to enable it, so every HTTP request runs the full pipeline.

## Error paths

At the module boundary (`run_fast_retrieve`):

| Failure | Result |
|---|---|
| Empty query / non-positive limit | `empty_query` / `invalid_limit`, 400 |
| Encoding timeout or TEI failure | `embeddings_unavailable`, 503 (`retry_after_s=5` on the error) |
| Qdrant query failure | `index_unavailable`, 503 |
| One plane times out | success with the surviving planes + warning `plane_timeout_<plane>` |
| One plane errors | success with the surviving planes + warning `plane_error_<plane>` |
| All planes time out | `all_planes_timeout`, 503 (`retry_after_s=5` on the error) |
| Healthy, nothing matches | success with empty results, no warning |

Over HTTP the orchestrator maps a fast-path 503 to `kind="timeout"`, which the router returns as 503 `BACKEND_UNAVAILABLE`. When a request fans out across several targets and only some fail this way, the response is 200 with the surviving results and a `plane_timeout_<plane>` warning per failed target. The retrieve router does not currently send a `Retry-After` header.

## Voice integration notes

Voice adapters (for example `sourceblender/musubi-livekit`) use the fast path for anything that has to surface mid-response, and the deep path for context they can fetch ahead of time.

Call shape:

```python
results = await musubi.retrieve(
    RetrievalQuery(
        namespace="alex/voice/episodic",  # one concrete target
        query_text=user_utterance,
        mode="fast",
        limit=5,
    )
)
```

5 results is typically enough for in-conversation fact lookups ("what's the release window for the billing service?"). Larger results bloat the voice context.

## Observability

The retrieval metrics that exist are shared by every mode (`src/musubi/observability/retrieval_metrics.py`):

- `musubi_retrieval_warnings_total{warning, plane}` — degradation warnings on successful requests, e.g. `plane_timeout_concept`.
- `musubi_retrieval_errors_total{kind}` — total-failure requests by error kind.

There is no fast-path latency histogram, cache-hit counter or empty-result counter. Latency is visible through tracing (`retrieve.orchestration` span) when OpenTelemetry export is configured.

## Test Contract

**Module under test:** `src/musubi/retrieve/fast.py`, `src/musubi/retrieve/orchestration.py`

Happy path:

1. `test_fast_path_p50_under_150ms_on_10k_corpus` (smoke test against a mocked hybrid search, not a corpus benchmark)
2. `test_fast_path_returns_results_in_score_desc`
3. `test_fast_path_applies_namespace_filter`
4. `test_fast_path_applies_state_matured_default`
5. `test_fast_path_runs_planes_concurrently` (instrumented)

Degradation:

6. `test_fast_path_timeout_on_one_plane_returns_partial_with_warning`
7. `test_fast_path_tei_timeout_returns_503`
8. `test_fast_path_qdrant_down_returns_503`
9. `test_fast_path_empty_corpus_returns_empty_200`
10. `test_fast_path_all_planes_timeout_warns_all_planes`

Response cache (module only; not wired into the API):

11. `test_fast_path_response_cache_hits_within_30s`
12. `test_fast_path_response_cache_disabled_by_default`
13. `test_fast_path_embedding_cache_always_on` (per-call cache)

Correctness:

14. `test_fast_path_snippet_max_200_chars`
15. `test_fast_path_lineage_summary_present_not_hydrated`
16. `test_fast_path_does_not_call_reranker` (static import check)
17. `test_fast_path_dedupes_same_object_id_by_highest_score`

Property:

18. `test_hypothesis_same_query_on_same_corpus_returns_identical_results`
19. `test_hypothesis_limit_parameter_is_honored_exactly`

Integration (deferred: skipped stubs):

20. `test_integration_livekit_fast_talker_scenario_voice_like_queries_p95_400ms` — deferred
21. `test_integration_degradation_scenario_kill_sparse_tei_mid_request_response_still_returns_with_warnings` — deferred

Cold-query budget:

22. `test_fast_path_encodes_once_before_per_plane_timeout`
23. `test_fast_path_shares_encoding_with_real_hybrid_search`
24. `test_fast_path_bounds_cold_encoding_before_fanout`
25. `test_fast_path_uses_independent_encoding_and_search_deadlines`
26. `test_fast_timing_override_reaches_pipeline_and_whole_call`
27. `test_fast_deadline_env_overrides_are_independent`
28. `test_fast_deadlines_reject_nonpositive_values`
29. `test_retrieve_http_passes_configured_fast_deadlines`
