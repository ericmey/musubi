---
title: Reranker
section: 05-retrieval
tags: [cross-encoder, deep-path, rerank, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
---
# Reranker

A cross-encoder that scores (query, passage) pairs directly. Used only on the deep path — the latency cost is too high for fast path, but the quality lift on ambiguous queries is substantial.

## Model

**BGE-reranker-v2-m3** (`BAAI/bge-reranker-v2-m3`). April 2026 state-of-the-art for open-weight rerankers, 568M params, multilingual, beats larger proprietary models on MTEB reranking benchmarks for our size range.

Deployed via TEI in a dedicated instance (can co-load with BGE-M3 on the same GPU — they share the same tokenizer and VRAM isn't tight at our batch sizes). See [[08-deployment/gpu-inference-topology]].

## When it runs

Only on the deep path:

```python
if query.mode == "deep":
    candidates = await hybrid_fanout(query)   # 50-100 candidates
    reranked = await rerank(query.query_text, candidates, top_k=query.limit * 3)
    scored = [score(c) for c in reranked]
    packed = pack(scored[:query.limit])
```

Candidate count going into rerank: ~50-100 (3-4 planes × prefetch_limit = 20-30 per plane, after merge ~60-100). Reranker batch-processes all at once.

## The call

```python
async def rerank(
    query_text: str,
    candidates: list[Hit],
    *,
    top_k: int,
) -> list[Hit]:
    pairs = [(query_text, c.content_for_rerank) for c in candidates]
    scores = await tei_client.rerank(model="bge-reranker-v2-m3", pairs=pairs)
    for cand, score in zip(candidates, scores):
        cand.rerank_score = score
    # Sort and clip
    ranked = sorted(candidates, key=lambda c: c.rerank_score, reverse=True)
    return ranked[:top_k]
```

`content_for_rerank` is:

- For episodic / concept / curated: `f"{hit.title or ''}\n\n{hit.content[:2048]}"`
- For artifact chunks: `hit.chunk_content` verbatim

Truncation at 2048 chars (~1000 tokens) for the passage side keeps batch size manageable. The reranker's max context is 8K, so we could go longer — we don't, because it bloats latency with little recall gain on a household corpus.

## Latency budget

On the RTX 3080 via TEI, warm:

| Candidates | p50 | p95 |
|---|---|---|
| 20 | 80ms | 150ms |
| 50 | 180ms | 320ms |
| 100 | 340ms | 600ms |

So deep-path budget (p50 ≤ 2s) absorbs rerank at 100 candidates comfortably. Fast path (150ms p50) can't.

## How we use the rerank score

**The rerank score replaces the `relevance` component of the composite score.** It does not join as a 6th component. Why: it's already a measure of relevance (a much better one), and adding it alongside RRF-relevance double-counts.

Implementation: when a deep-path result has `rerank_score`, `_relevance()` returns a normalization of that instead of the RRF score.

```python
def _relevance(hit: Hit) -> float:
    if hit.rerank_score is not None:
        return hit.rerank_score_normalized
    return hit.rrf_score / hit.batch_max_rrf
```

`rerank_score_normalized` is a sigmoid over the raw cross-encoder logit, so the value lands in [0, 1] consistently.

## When we skip reranking on deep path

- Candidate count ≤ 5: the hybrid result is tiny; no reorder helpful.
- Candidate count == 0: trivial no-op.
- TEI reranker instance down: fall back to RRF-only relevance + log a warning.

## Multi-plane reranking

Reranker scores are **plane-agnostic**. We pass everything to the reranker as a flat list (not per-plane), then score + sort. A curated fact and an episodic memory compete on the same query-passage score; the provenance component re-introduces plane preference at scoring time.

## Batching

TEI batches internally by default. For a single query with 50 candidates we send one request with 50 pairs; TEI packs them into sub-batches sized to fit the GPU's compute tile.

We don't cross-batch queries (one reranker request per user query). Attempting to batch across queries would require request-queueing and would introduce head-of-line blocking.

## Quality expectation

Empirically (BEIR + MTEB reranking at April 2026):

- Hybrid BGE-M3+SPLADE retrieval, no rerank: NDCG@10 ≈ 0.52–0.58 on heterogeneous corpora.
- Same + BGE-reranker-v2-m3 rerank top-100: NDCG@10 ≈ 0.64–0.70.

That's the gap we're paying rerank latency for. On queries where the hybrid retrieval already ranks the answer in top-3, rerank contributes little; on ambiguous queries, it's meaningful.

We'll measure our own corpus via [[05-retrieval/evals]] and adjust if the win is smaller than expected.

## Test contract

**Module under test:** `musubi/retrieval/rerank.py`

1. `test_rerank_sorts_by_cross_encoder_score`
2. `test_rerank_replaces_relevance_component` (not appends)
3. `test_rerank_skipped_when_candidates_le_5`
4. `test_rerank_degrades_to_rrf_when_tei_down`
5. `test_rerank_content_truncated_to_2048_chars`
6. `test_rerank_score_normalized_via_sigmoid`
7. `test_rerank_called_only_on_deep_path` (mode=deep; mode=fast asserts not called)
8. `test_rerank_latency_under_budget_for_50_candidates` (benchmark)
9. `test_rerank_plane_agnostic_ordering`

Degradation:

10. `test_rerank_tei_error_returns_hybrid_results_with_warning`
11. `test_rerank_partial_batch_failure_rescored_for_rest`

Integration:

12. `integration: deep-path NDCG@10 on golden set improves vs fast-path by ≥ 5 points`
13. `integration: deep-path p95 latency under 2s with 100 candidates`
