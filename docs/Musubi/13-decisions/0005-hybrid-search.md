---
title: "ADR 0005: Hybrid Dense + Sparse + Reranker from Day One"
section: 13-decisions
tags: [adr, retrieval, search, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0005: Hybrid Dense + Sparse + Reranker from Day One

**Status:** accepted
**Date:** 2026-03-15
**Deciders:** Eric

## Context

Retrieval options for v1:

1. **Dense only.** BGE-M3 embeddings, cosine similarity. Semantically smart but weak on exact-token queries ("jwt_secret", proper nouns, numbers).
2. **Sparse only (BM25 or SPLADE).** Good for exact tokens; misses semantic paraphrase.
3. **Hybrid dense + sparse.** Fuse both. Qdrant 1.10+ supports named vectors with server-side RRF fusion.
4. **Hybrid + reranker.** Pull a wider candidate set via hybrid, then re-score with a cross-encoder that looks at query+doc pairs. Significantly better NDCG on public benchmarks (BEIR, MTEB) as of 2026.

Empirical evidence (public benchmarks through April 2026):

- BGE-M3 alone on BEIR: ~0.52 nDCG@10 avg.
- BGE-M3 + SPLADE++ V3 RRF: ~0.55.
- + BGE-reranker-v2-m3 on top 50: ~0.60.

That last jump (+5 nDCG) is large and underused.

The cost of doing this later instead of now:

- Dense only → hybrid = re-embed or add a second vector, a migration ([[11-migration/phase-2-hybrid-search]]).
- Adding rerank = easy code-side, but you miss its benefits until it ships.

Since re-embedding is a relatively heavy operation and the tooling to avoid it (named vectors, additive rollout) is exactly what we're building, the v1 target includes all three.

## Decision

v1 retrieval uses **dense + sparse + cross-encoder reranker** from ship day:

- **Dense:** `dense_bge_m3_v1` (BGE-M3, 1024-d).
- **Sparse:** `sparse_splade_v1` (SPLADE++ V3, sparse vector).
- **Fusion:** Qdrant server-side RRF via `FusionQuery(fusion=Fusion.RRF)` with `Prefetch` per named vector.
- **Rerank:** BGE-reranker-v2-m3 on top-50 candidates, down to top-N.

The stack is pipelined: ANN → RRF → rerank → return. Users choose "fast" (no rerank) or "deep" (rerank) via mode flag.

## Alternatives

**A. Dense only, add sparse+rerank in v2.** Rejected: re-embedding tooling is a v1 deliverable anyway; shipping dense-only buys nothing and defers the harder benchmarking work.

**B. Sparse only (lexical BM25).** Fine for code-search-style queries; loses semantic value.

**C. ColBERT-style late interaction.** Strong results, but more infra (MaxSim scoring, larger indexes). Not worth it at our scale in v1.

**D. LLM-based reranking (query LLM per candidate).** Much more expensive, slower; marginally better than cross-encoder in quality. Not worth the latency for interactive retrieval.

## Consequences

- Two embedding models + a reranker to deploy ([[08-deployment/gpu-inference-topology]]). VRAM budget includes all three.
- Named-vector schema from day one; re-embedding path ([[11-migration/re-embedding]]) works uniformly for future model swaps.
- Retrieval latency target: fast-mode p95 < 400ms, deep-mode p95 < 5s ([[12-roadmap/phased-plan#v1-target-metrics]]).
- Eval suite must include recall@k AND rerank quality (MRR, nDCG).

Trade-offs:

- More moving parts = more ops surface. TEI for embeddings + TEI-reranker for cross-encoder.
- GPU budget is tight (~9.6GB of 10GB VRAM). Leaves little room for model upgrades without a GPU change. See [[11-migration/scaling]].

## Links

- [[13-decisions/0006-pluggable-embeddings]]
- [[05-retrieval/hybrid-search]]
- [[05-retrieval/reranker]]
- [[11-migration/phase-2-hybrid-search]]
- [[11-migration/phase-3-reranker]]
