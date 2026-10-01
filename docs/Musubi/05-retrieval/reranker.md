---
title: Reranker
section: 05-retrieval
tags: [cross-encoder, deep-path, rerank, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/retrieve/rerank.py", "tests/retrieve/test_rerank.py"]
---
# Reranker

A cross-encoder that scores (query, passage) pairs directly. Used only on the deep path (and so in blended, which runs deep per plane) — the latency cost is too high for the fast path, but the quality lift on ambiguous queries is substantial.

## Model

**BGE-reranker-v2-m3** (`BAAI/bge-reranker-v2-m3`): an open-weight, multilingual cross-encoder of about 568M parameters.

Deployed via TEI in a dedicated instance (it can share a GPU with BGE-M3; VRAM isn't tight at our batch sizes). See [[08-deployment/gpu-inference-topology]].

## When it runs

Only on the deep path (`src/musubi/retrieve/deep.py`):

```python
# per deep call
candidates = merge(hybrid_search(plane, limit=query.limit * 2) for plane in planes)
reranked = await rerank(reranker, query.query_text, candidates, top_k=query.limit)
scored = rank_hits(reranked, now=now)
```

Each plane's hybrid search fetches `query.limit * 2` candidates; the merged set (deduped by `object_id`) goes to the reranker, which keeps `top_k = query.limit`. Through `/v1/retrieve` the orchestrator runs one deep call per `(namespace, plane)` target, so in practice each rerank call sees one plane's candidates. The fast path never reranks.

## The call

`rerank()` in `src/musubi/retrieve/rerank.py`:

```python
async def rerank(
    client: TEIRerankerClient,
    query_text: str,
    candidates: list[Hit],
    *,
    top_k: int,
) -> RerankResult:
    if len(candidates) <= 5:
        return RerankResult(hits=candidates[:top_k])
    texts = [_extract_content(c) for c in candidates]
    scores = await client.rerank(query_text, texts)
    scored = [replace(c, rerank_score=s) for c, s in zip(candidates, scores)]
    ranked = sorted(scored, key=lambda c: c.rerank_score, reverse=True)
    return RerankResult(hits=ranked[:top_k])
```

The rerank text is:

- For episodic / concept / curated: `f"{title}\n\n{content[:2048]}"` (content only when there is no title)
- For artifact chunks: `chunk_content` verbatim

Truncation at 2048 characters keeps batches manageable. The reranker's context is longer; we don't use it, because it adds latency for little recall gain on a small-team corpus.

## Latency budget

The rerank stage is bounded by `retrieval_rerank_timeout_s` (default 1.5 s, `src/musubi/settings.py`). On expiry, deep retrieval returns the fused (RRF) order with a `reranker_failed` warning (`cause="timeout"`) instead of failing the request. The default comes from a ten-caller burst on the reference deployment (see [[05-retrieval/deep-path]]).

Per-candidate-count latency depends on the host; measure on yours. On the measured reference host (RTX 3080 10 GB) reranking is fast enough for the 5 s deep budget and far too slow for the 400 ms fast budget.

## How we use the rerank score

**The rerank score replaces the `relevance` component of the composite score.** It does not join as a 6th component. Why: it's already a measure of relevance (a much better one), and adding it alongside RRF-relevance double-counts.

Implementation: when a hit has `rerank_score`, `_relevance()` returns its sigmoid instead of the normalized RRF score (`src/musubi/retrieve/scoring.py`):

```python
def _relevance(hit: Hit) -> float:
    if hit.rerank_score is not None:
        return sigmoid(hit.rerank_score)
    if hit.batch_max_rrf <= 0.0:
        return 0.0
    return clamp01(hit.rrf_score / hit.batch_max_rrf)
```

The sigmoid maps the raw cross-encoder logit into [0, 1].

## When we skip reranking on deep path

- Candidate count ≤ 5: the hybrid result is tiny; no reorder helpful. No warning.
- Candidate count == 0: deep returns early; nothing to rerank.
- TEI reranker error or stage timeout: fall back to the fused RRF order (`hybrid_fallback`, sorted by `(-rrf_score, object_id)`) + a `reranker_failed` warning with a bounded `cause` (`timeout`, `request_rejected`, `unavailable`, `invalid_response`, `unexpected_error`).

## Multi-plane reranking

Reranker scores are **plane-agnostic**: plane does not influence the cross-encoder score. The provenance component re-introduces plane preference at scoring time.

## Batching

`TEIRerankerClient.rerank` (`src/musubi/embedding/tei.py`) splits the candidates into chunks of the reranker's `max_client_batch_size`, discovered from the TEI `/info` endpoint at startup (fallback 32), and sends one `/rerank` request per chunk. If any chunk fails, the whole rerank degrades to the RRF order; there is no partial rescoring. See [[05-retrieval/reranker-batching]].

We don't cross-batch queries (one reranker request stream per query). Batching across queries would require request-queueing and would introduce head-of-line blocking.

## Quality expectation

On queries where hybrid retrieval already ranks the answer in the top 3, rerank contributes little; on ambiguous queries, it's meaningful. We measure our own corpus via [[05-retrieval/evals]] (the deep-mode nightly gate) and adjust if the win is smaller than expected.

## Test Contract

**Module under test:** `src/musubi/retrieve/rerank.py`

1. `test_rerank_sorts_by_cross_encoder_score`
2. `test_rerank_replaces_relevance_component` (not appends)
3. `test_rerank_skipped_when_candidates_le_5`
4. `test_rerank_degrades_to_rrf_when_tei_down`
5. `test_rerank_content_truncated_to_2048_chars`
6. `test_rerank_score_normalized_via_sigmoid`
7. `test_rerank_called_only_on_deep_path` — deferred (skipped stub; covered by `test_fast_mode_skips_rerank` in [[05-retrieval/orchestration]])
8. `test_rerank_latency_under_budget_for_50_candidates` (smoke test with a fake client)
9. `test_rerank_plane_agnostic_ordering`

Degradation:

10. `test_rerank_tei_error_returns_hybrid_results_with_warning`
11. `test_rerank_partial_batch_failure_rescored_for_rest` (despite the name, asserts that any client error degrades the whole rerank: no hit keeps a rerank score)
12. `test_reranker_batch_failure_degrades_the_whole_rerank`

Integration:

13. `test_integration_deep_path_p95_latency_under_2s_with_100_candidates` — deferred (skipped stub)
