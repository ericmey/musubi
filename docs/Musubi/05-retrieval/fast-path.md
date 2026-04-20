---
title: Fast Path
section: 05-retrieval
tags: [fast-path, latency, retrieval, section/retrieval, status/complete, type/spec, voice]
type: spec
status: complete
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/fast.py", "tests/retrieve/test_fast.py"]
---
# Fast Path

Retrieval for voice, chat, and any surface where a human is actively waiting. Budget: p50 ≤ 150ms, p95 ≤ 400ms end-to-end, including network round trip from adapter.

## The plan

```
adapter.query()
  │ 1. validate + authorize             ~1ms
  │ 2. encode query (dense + sparse)    ~40ms warm
  │ 3. fan-out hybrid per-plane         ~70ms (concurrent)
  │ 4. merge + dedup + score            ~5ms
  │ 5. pack top-K with brief snippets   ~3ms
  └─► response                          ~ 120ms median
```

Five steps, no reranker, no LLM, no graph traversal. Every step has a timeout and a degradation.

## Per-step details

### Step 1: Validate + authorize

- Parse `RetrievalQuery` (pydantic).
- Check token scope against `namespace`.
- Reject if `mode != "fast"` and this endpoint is fast-only.

Budget: 1ms. Failure: 400/403.

### Step 2: Query encoding

- Check in-memory LRU cache (see [[05-retrieval/hybrid-search#caching]]).
- On miss: parallel HTTP to TEI for dense + sparse.
- Timeout: 80ms total; on timeout, degrade to whichever completed.

Budget: 20–50ms. Failure: if both models timeout, 503. (Very rare — TEI is colocated.)

### Step 3: Hybrid fan-out

For each plane in the query (default: `[curated, concept, episodic]`):

- Submit hybrid query to Qdrant with namespace filter + state filter + user filters.
- `limit = K_pre` where `K_pre = max(20, query.limit * 2)` so we have headroom for merge/dedup.
- Timeout: 250ms per-collection.

All plane queries run **concurrently** via `asyncio.gather(return_exceptions=True)`. A failed or slow plane doesn't block others.

Budget: 70ms (concurrent; dominated by slowest plane). Failure: if all planes return empty/error, 200 with empty results + a soft warning in the response.

### Step 4: Merge + dedup + score

See [[05-retrieval/blended]] for the full merge algorithm. Key moves for fast path:

- **Content-similarity dedup** is approximate (hash of first 200 chars + tag-set Jaccard ≥ 0.5). Fast, good-enough. We don't rerun cosine in fast path.
- **Lineage-aware drop**: if a concept `C.promoted_to == Q.object_id` for any curated `Q` in the result set, drop `C`.
- **Score** using [[05-retrieval/scoring-model]] (single pass, no sorting the hits twice).

Budget: 5ms on a 60-hit batch.

### Step 5: Pack response

- Sort by score desc.
- Take top `query.limit`.
- Generate `snippet`: first 200 chars of content, falling back to title if content is already short.
- Compute `lineage_summary` from payload (cheap — it's already in the payload).
- Return.

Budget: 3ms.

## What fast path doesn't do

- **No cross-encoder rerank.** Those add 150–400ms even on GPU; they kill our budget.
- **No LLM rewriting of queries.** The raw user text is the query.
- **No lineage hydration.** We include a `lineage_summary` from payload but don't fetch superseded objects, chunk contents, or citation targets.
- **No cache of full responses.** We cache query embeddings only. Payload-level caching would bloat memory for a small hit rate (queries vary more than embeddings).

## Optional response cache

For exact-query repeats within a short window (e.g., a voice agent gets interrupted and re-queries the same thing), a 30-second TTL cache keyed on `(namespace, query_text, filter_hash, mode)` can short-circuit the whole pipeline.

Disabled by default (`FAST_PATH_RESPONSE_CACHE=false`). Enable per-adapter as needed.

## Error paths

| Failure | Response |
|---|---|
| TEI down | 503 with `detail: "embeddings unavailable"` + `retry-after: 5` header |
| Qdrant down | 503 with `detail: "index unavailable"` |
| One plane timeout | 200 with partial results + `warnings: ["plane: concept timed out"]` |
| All planes timeout | 200 with empty results + `warnings: ["all planes timed out"]` — caller decides whether to retry |
| Unauthorized | 403 with `detail: "namespace not in token scope"` |
| Malformed query | 400 with pydantic validation errors |

## LiveKit integration notes

The voice agent's **Slow Thinker** pattern pre-fetches context during user turn; it's on the deep path. The **Fast Talker** uses this fast path for anything that surfaces mid-response.

Fast Talker call shape:

```python
results = await musubi.retrieve(
    RetrievalQuery(
        namespace="eric/livekit-voice/blended",     # see blended.md — blended scope
        query_text=user_utterance,
        mode="fast",
        limit=5,
    )
)
```

5 results is typically enough for in-conversation fact lookups ("what GPU is in the musubi host?"). Larger results bloat the voice context.

## Observability

Fast path is hot. We emit metrics per request:

- `retrieval.fast.latency_ms` (histogram; labels: `namespace`, `plane_count`)
- `retrieval.fast.cache_hit` (counter; labels: `layer: embedding|response`)
- `retrieval.fast.plane_timeout` (counter; labels: `plane`)
- `retrieval.fast.empty_results` (counter; labels: `namespace`)
- `retrieval.fast.result_count` (histogram)

Grafana dashboard: p50, p95, cache hit rate, empty-result rate per namespace. Alerts on p95 > 500ms for 5m.

## Test Contract

**Module under test:** `musubi/retrieval/fast.py`, `musubi/retrieval/orchestration.py`

Happy path:

1. `test_fast_path_p50_under_150ms_on_10k_corpus` (benchmark)
2. `test_fast_path_returns_results_in_score_desc`
3. `test_fast_path_applies_namespace_filter`
4. `test_fast_path_applies_state_matured_default`
5. `test_fast_path_runs_planes_concurrently` (instrumented)

Degradation:

6. `test_fast_path_timeout_on_one_plane_returns_partial_with_warning`
7. `test_fast_path_tei_timeout_returns_503`
8. `test_fast_path_qdrant_down_returns_503`
9. `test_fast_path_empty_corpus_returns_empty_200`

Feature flags:

10. `test_fast_path_response_cache_hits_within_30s`
11. `test_fast_path_response_cache_disabled_by_default`
12. `test_fast_path_embedding_cache_always_on`

Correctness:

13. `test_fast_path_snippet_max_200_chars`
14. `test_fast_path_lineage_summary_present_not_hydrated`
15. `test_fast_path_does_not_call_reranker` (mock + assert not called)

Property:

16. `hypothesis: same query on same corpus returns identical results`
17. `hypothesis: limit parameter is honored exactly`

Integration:

18. `integration: LiveKit Fast-Talker scenario: voice-like queries p95 ≤ 400ms`
19. `integration: degradation scenario — kill sparse TEI mid-request, response still returns with warnings`
