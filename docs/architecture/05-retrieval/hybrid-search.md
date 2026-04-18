---
title: Hybrid Search
section: 05-retrieval
tags: [dense, hybrid, retrieval, rrf, section/retrieval, sparse, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
---
# Hybrid Search

Dense + sparse, fused server-side by Reciprocal Rank Fusion. Every retrieval in Musubi goes through this — fast path, deep path, blended cross-plane — all start with the same hybrid step.

## Why hybrid (not just dense)

Dense embeddings (BGE-M3) are strong on **semantic** matches — "tell me about GPU setup" pulls files about CUDA, drivers, nvidia-container-toolkit even when none of those words match the query.

Sparse embeddings (SPLADE++) are strong on **lexical** matches — a rare token like `nvidia-container-toolkit` hits the exact file that mentions it by name, even when semantic similarity is weak.

Neither is a superset of the other. A BEIR-style evaluation ([https://arxiv.org/abs/2104.08663](https://arxiv.org/abs/2104.08663)) consistently shows hybrid + RRF at 2–7 points NDCG@10 above either alone on heterogeneous corpora. Our corpus is heterogeneous (code, prose, transcripts, runbooks), so hybrid pays.

See [[13-decisions/0005-hybrid-search]] for the decision record.

## Models

**Dense**: BGE-M3 (1024-d, cosine). Runs on GPU via Text Embeddings Inference (TEI). April 2026 model: `BAAI/bge-m3` — multilingual, 8K context, strong both on short queries and long passages.

**Sparse**: SPLADE++ V3 (`naver/splade-cocondenser-ensembledistil` or later). Produces term-weight dictionaries. Fits in ~700MB VRAM on the RTX 3080.

Both models are pinned in TEI at boot (see [[08-deployment/gpu-inference-topology]]).

Why not e5 or GTE? BGE-M3 beats both on BEIR at similar size, and its sparse companion (BGE-M3 itself can emit sparse vectors) is an option — but SPLADE++ V3 outperforms BGE-M3's sparse head on retrieval quality in our April 2026 benchmarks. We use SPLADE++ for sparse.

## Fusion: server-side RRF

Qdrant 1.15+ supports server-side RRF fusion via `FusionQuery`:

```python
client.query_points(
    collection_name="musubi_episodic",
    prefetch=[
        models.Prefetch(
            query=dense_vector,
            using="dense_bge_m3_v1",
            limit=50,
        ),
        models.Prefetch(
            query=sparse_vector,
            using="sparse_splade_v1",
            limit=50,
        ),
    ],
    query=models.FusionQuery(fusion=models.Fusion.RRF),
    query_filter=filter,
    limit=20,
    with_payload=True,
)
```

RRF formula, for reference:

```
rrf_score(d) = sum over rankers r of 1 / (k + rank_r(d))
```

with `k = 60` (Qdrant default). RRF is rank-based (not score-based), so we don't need to normalize dense-cosine and sparse-dot-product into the same space — a key ergonomic win.

Alternatives we evaluated:

- **Weighted sum of scores.** Requires per-corpus score normalization. Brittle.
- **CombSUM / CombMNZ.** Similar issues.
- **Learned fusion (LTR).** Worth it at scale; overkill for a household corpus.

RRF it is.

## Configuring the prefetch step

Each prefetch `limit` is 50 (configurable, `HYBRID_PREFETCH_LIMIT`). Rationale:

- Too low: poor recall — RRF can't lift a hit into top-20 if neither ranker returned it.
- Too high: latency bloat for no retrieval gain past ~50 (diminishing returns per BEIR tests).
- 50 balances p50 latency (~60ms on the reference host for both prefetches in parallel) with recall.

The final `limit` (20 by default) is the returned result count after fusion.

## Query encoding

Query encoding happens in the Core, not in TEI (which is model inference only):

```python
async def encode_query(text: str) -> tuple[list[float], SparseVector]:
    dense_task = tei_client.embed_dense(model="bge-m3", text=text)
    sparse_task = tei_client.embed_sparse(model="splade-v3", text=text)
    dense, sparse = await asyncio.gather(dense_task, sparse_task)
    return dense, sparse
```

Parallel HTTP calls to TEI. Both return ~30ms on hot cache for short queries.

## Caching

A small query-embedding cache in Core:

```python
# In-memory LRU, 10K entries
@lru_cache(maxsize=10_000)
def query_embedding_cache(query_text: str) -> tuple[list[float], SparseVector]: ...
```

Hit rate is meaningful for assistant-y workloads — users repeat "what's on my plate today" type queries. Cache is keyed on the raw query text; we don't normalize (lowercasing changes embeddings). Misses fall through to TEI.

Cache invalidation: model swap → cache cleared at boot. Not something we need runtime invalidation for.

## Filter pushdown

The filter goes in the same `query_points` call as `query_filter`. Qdrant evaluates it against the candidate set — filters are not applied after fusion.

Most-frequent filter: `namespace` (always set). Index hit rate on this field must be ~100% — it's the first gate.

Secondary filters: `state IN (matured, promoted)`, `tags`, `topics`, `created_epoch BETWEEN ...`. All indexed; see [[04-data-model/qdrant-layout]].

## Multi-collection queries

Hybrid search is per-collection. When a `RetrievalQuery.planes` spans multiple, we **fan out** one query per collection in parallel, then **merge and re-score** in Core. See [[05-retrieval/blended]].

Why not one Qdrant query over multiple collections? Qdrant doesn't support cross-collection search in a single call; collections are independent indexes. Fan-out + merge is our answer.

## Query timeouts

Every hybrid call has a hard timeout:

- Fast path: 250ms per-collection
- Deep path: 1500ms per-collection

On timeout: return what we have (empty if no prefetch has completed), log a `retrieval.timeout` metric, fall back to dense-only if sparse is the slow path. We don't block on a slow sparse vector encode — the user gets *something* under budget.

## Local-inference budgets

TEI + our reference host:

| Operation | p50 | p95 | Notes |
|---|---|---|---|
| BGE-M3 encode (query, ≤ 64 tokens) | 20ms | 45ms | warm, INT8 quant |
| SPLADE++ encode (query) | 25ms | 55ms | warm |
| Both in parallel | 30ms | 60ms | dominated by slower |
| Qdrant hybrid query (50+50 prefetch, 100K corpus) | 25ms | 70ms | after warmup |
| Total encode + search | 60ms | 130ms | |

These numbers hold for our corpus size; see [[05-retrieval/evals]] for benchmarks.

## Test contract

**Module under test:** `musubi/retrieval/hybrid.py`, `musubi/retrieval/embed.py`

1. `test_hybrid_query_uses_both_prefetch_steps`
2. `test_rrf_fusion_requested_server_side`
3. `test_namespace_filter_always_applied`
4. `test_prefetch_limit_comes_from_config`
5. `test_empty_query_returns_empty_not_error`
6. `test_query_encoding_runs_in_parallel` (instrumented)
7. `test_query_embedding_cache_hit_on_repeat`
8. `test_cache_cleared_on_model_version_change`
9. `test_hybrid_timeout_returns_partial_results`
10. `test_dense_only_fallback_when_sparse_timeout`
11. `test_fanout_over_planes_parallel` (instrumented)
12. `test_results_deduped_within_single_collection`
13. `test_filter_state_matured_excludes_archived_by_default`
14. `test_include_archived_opts_in`

Property tests:

15. `hypothesis: RRF result is deterministic for fixed (seed, corpus, query)`
16. `hypothesis: increasing prefetch_limit never reduces recall on fixed query`

Integration:

17. `integration: BEIR-style eval on 1000-doc synthetic corpus, hybrid beats dense-only by ≥ 2 NDCG@10 points`
18. `integration: live Qdrant, hybrid with real BGE-M3 + SPLADE, p95 ≤ 150ms`
