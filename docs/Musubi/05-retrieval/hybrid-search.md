---
title: Hybrid Search
section: 05-retrieval
tags: [dense, hybrid, retrieval, rrf, section/retrieval, sparse, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: "tests/retrieve/test_hybrid.py"
---
# Hybrid Search

Dense + sparse, fused server-side by Reciprocal Rank Fusion. Every ranked retrieval in Musubi goes through this — fast, deep and blended all start with the same hybrid step (`src/musubi/retrieve/hybrid.py`). Only `recent` mode skips it.

## Why hybrid (not just dense)

Dense embeddings (BGE-M3) are strong on **semantic** matches — "how do we ship a release" pulls notes about deploy checklists and rollbacks even when none of those words match the query.

Sparse embeddings (SPLADE) are strong on **lexical** matches — a rare token like an error code or a package name hits the exact note that mentions it, even when semantic similarity is weak.

Neither is a superset of the other. A BEIR-style evaluation ([https://arxiv.org/abs/2104.08663](https://arxiv.org/abs/2104.08663)) consistently shows hybrid + RRF at 2–7 points NDCG@10 above either alone on heterogeneous corpora. Our corpus is heterogeneous (code, prose, transcripts, runbooks), so hybrid pays.

See [[13-decisions/0005-hybrid-search]] for the decision record.

## Models

**Dense**: BGE-M3 (`BAAI/bge-m3`, 1024-d, cosine) via Text Embeddings Inference (TEI) — multilingual, 8K context, strong both on short queries and long passages.

**Sparse**: SPLADE v3 (`naver/splade-v3`, the `SPARSE_MODEL` default in `.env.example`) via TEI. Produces term-weight dictionaries. Fits in ~700 MB VRAM on the measured reference host (RTX 3080 10 GB).

Both models are pinned in TEI at boot (see [[08-deployment/gpu-inference-topology]]).

Why not e5 or GTE? BGE-M3 is competitive with both on BEIR at similar size. BGE-M3 can also emit sparse vectors itself; we use a dedicated SPLADE model for the sparse channel instead.

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
- **Learned fusion (LTR).** Worth it at scale; overkill for a small-team corpus.

RRF it is.

### Channel controls are booleans, not weights

The internal `hybrid_search` and `hybrid_search_many` seams expose
`dense_enabled` and `sparse_enabled` booleans for diagnostic and degradation
paths. They do not expose numeric weights: Qdrant's server-side RRF is unweighted,
so accepting a magnitude would advertise tuning that the fusion cannot honor.

Both channels default to enabled. A collection without a sparse vector capability
uses dense only under that default. A caller explicitly requesting sparse-only on
such a collection receives the typed `no_retrieval_channels` error; Musubi does
not silently turn an unavailable requested mode into an empty query.

## Configuring the prefetch step

Each prefetch `limit` is 50: the module constant `HYBRID_PREFETCH_LIMIT` in `src/musubi/retrieve/hybrid.py`. Callers can pass `prefetch_limit` to `hybrid_search`; there is no settings field for it. Rationale:

- Too low: poor recall — RRF can't lift a hit into top-20 if neither ranker returned it.
- Too high: latency bloat for diminishing retrieval gain.
- 50 balances latency with recall.

The final `limit` is the returned result count after fusion; the fast and deep paths pass their own (see [[05-retrieval/fast-path]], [[05-retrieval/deep-path]]).

## Query encoding

Core asks TEI for both encodings concurrently (`_encode_query` in `src/musubi/retrieve/hybrid.py`): the dense and sparse requests are started as separate tasks and awaited together. If a `sparse_timeout_s` is set and the sparse request exceeds it, the query continues dense-only with a `sparse_embedding_failed` warning. A dense failure, or a non-timeout sparse failure, is an error (`dense_embedding_failed` / `sparse_embedding_failed`). A collection without a sparse vector skips the sparse request.

## Caching

`QueryEmbeddingCache` is an in-process LRU (default `maxsize=10_000`), keyed on the raw query text and tagged with a model version; changing the model version clears it. It is not normalised (lowercasing changes embeddings).

**The cache is per request, not process-wide.** The fast path creates a new cache for each `run_fast_retrieve` call, so the plane searches inside that call share one encoding; the deep path and the orchestrator pass no cache at all. Nothing keeps embeddings across requests, so a repeated query re-encodes.

## Filter pushdown

The filter goes in the same `query_points` call as `query_filter`. Qdrant evaluates it against the candidate set — filters are not applied after fusion. **The exact-`namespace` top-level `query_filter` is the production scope: a real Qdrant server applies it to candidate generation, so exact scoping there is sufficient to keep a concrete target presence-exact (verified against a real server).** RET-011 additionally pushes the `namespace` scope onto each `prefetch` sub-query as **defense-in-depth and local-mode parity** — the in-memory (`:memory:`) test client does not apply the top-level fusion filter to prefetch+fusion results, so per-prefetch scoping is what lets unit tests observe the same behaviour a real server already gives.

Most-frequent filter: `namespace` (always set). Index hit rate on this field must be ~100% — it's the first gate.

> **Decision — #510 supersedes #332, for retrieval of a CONCRETE target only.** `namespace` is the **exact** deployment namespace (`tenant/presence/plane`); a concrete target returns only that presence's rows. The `identity_family` federation introduced by #332 (scoping to the first path segment so every presence of one identity was cross-visible) is reversed here for concrete-target retrieval. Cross-presence / identity-family retrieval is still supported, but ONLY when the request explicitly resolves to multiple concrete `namespace_targets` — i.e. a wildcard like `sam/*/episodic` expanded upstream by `retrieve._expand_wildcard_targets`, each concrete leg exact-filtered and unioned. **Unchanged:** wildcard-expanded multi-target retrieval, scope/auth wildcard matching, and lifecycle **synthesis** family federation (`lifecycle/synthesis.py`), which is intentionally identity-scoped. No ADR (per the routing decision); this note + Issue #510 + the discrimination tests are the record.

Secondary filter on ranked queries: lifecycle `state` (default `matured`, `promoted`; `include_archived` with no explicit `state_filter` removes the state restriction). For the episodic and curated collections, `state` is applied after the point is resolved to its authoritative row, and curated also applies its validity window there. `tags` and `since` are consumed only by `recent` mode. Indexes: see [[04-data-model/qdrant-layout]].

## Multi-collection queries

Hybrid search is per-collection. When a request spans multiple planes, Core **fans out** one query per collection in parallel and merges in Core. See [[05-retrieval/orchestration]] and [[05-retrieval/blended]].

Why not one Qdrant query over multiple collections? Qdrant doesn't support cross-collection search in a single call; collections are independent indexes. Fan-out + merge is our answer.

## Query timeouts

Every hybrid call has a timeout:

- Fast path: `retrieval_fast_plane_timeout_s` (default 0.25 s) per collection.
- Deep path: 1.5 s per collection, with sparse encoding bounded at 1.0 s.

The two timeouts behave differently:

- **Qdrant query timeout** → the call returns the error `qdrant_timeout`, never an empty success. The caller turns it into a `plane_timeout_<plane>` warning if other planes survived.
- **Sparse encoding timeout** → the call continues **dense-only** and carries a `sparse_embedding_failed` warning. We don't block on a slow sparse encode.

## Local-inference budgets

The fast-path target (p95 ≤ 400 ms end to end) assumes colocated inference: TEI and Qdrant on the same host as core. Per-operation figures are not published as guarantees; measure on your own host. The nightly eval gate tracks latency regressions (see [[05-retrieval/evals]]).

## Test Contract

**Module under test:** `src/musubi/retrieve/hybrid.py`, `src/musubi/embedding/`

1. `test_hybrid_query_uses_both_prefetch_steps`
2. `test_rrf_fusion_requested_server_side`
3. `test_namespace_filter_applied_not_identity_family`
4. `test_prefetch_limit_comes_from_config`
5. `test_empty_query_returns_empty_not_error` (asserts a typed `empty_query` error without querying Qdrant)
6. `test_query_encoding_runs_in_parallel` (instrumented)
7. `test_query_embedding_cache_hit_on_repeat`
8. `test_cache_cleared_on_model_version_change`
9. `test_hybrid_timeout_returns_err`
10. `test_dense_only_fallback_when_sparse_timeout`
11. `test_fanout_over_planes_parallel` (instrumented)
12. `test_results_deduped_within_single_collection`
13. `test_filter_state_matured_excludes_archived_by_default`
14. `test_include_archived_opts_in`

Property tests:

15. `test_hypothesis_rrf_result_is_deterministic_for_fixed_seed_corpus_query`
16. `test_hypothesis_increasing_prefetch_limit_never_reduces_recall_on_fixed_query`

RET-011 exact deployment-namespace consistency (#510) — realized in
`tests/retrieve/test_ret011_exact_namespace.py`, `tests/api/test_ret011_streaming_namespace.py`,
and `tests/retrieve/test_ret011_exact_namespace_integration.py`:

17. `test_concrete_target_does_not_leak_sibling_presence` (fast / deep / blended)
18. `test_recent_concrete_target_is_presence_exact`
19. `test_fast_cache_does_not_serve_sibling_presence`
20. `test_explicit_multi_target_still_returns_all_presences` (wildcard-expansion non-regression)
21. `test_streaming_concrete_target_is_presence_exact`
22. `test_concrete_target_exact_namespace_real_qdrant` (integration — real Qdrant proof)

Integration:

23. `test_integration_beir_style_eval_on_1000_doc_synthetic_corpus_hybrid_beats_dense_only_by_2_ndcg10_points`
24. `test_integration_live_qdrant_hybrid_with_real_bge_m3_splade_p95_150ms` — deferred (skipped stub)
