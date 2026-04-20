---
title: "Phase 3: Reranker"
section: 11-migration
tags: [migration, phase-3, reranker, section/migration, status/stub, type/migration-phase]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-2-hybrid-search]]"
next: "[[11-migration/phase-4-planes]]"
reviewed: false
---
# Phase 3: Reranker

Add the BGE-reranker-v2-m3 cross-encoder for the deep retrieval path.

## Goal

Deep retrieval produces markedly better ordering on the top-K via a cross-encoder rerank. Fast path unchanged.

## Changes

### TEI reranker service

Add a third TEI container running `BAAI/bge-reranker-v2-m3` in reranker mode. VRAM: ~1.4 GB. See [[08-deployment/gpu-inference-topology]].

### Retrieve pipeline

Introduce a `mode` field on `RetrievalQuery`:

- `fast` (existing): hybrid retrieval, no rerank. ≤ 150ms p50.
- `deep`: hybrid → top-50 → rerank → top-K. ~800ms-2s p50.

See [[05-retrieval/reranker]] for the scoring effect: rerank score **replaces** the relevance component, doesn't append.

### Flag gating

During rollout, `deep` is behind a flag. Callers that don't request it get `fast`. LiveKit Slow Thinker is the first real consumer.

### Shadow eval

Run both `fast` and `deep` on the same golden set (see [[05-retrieval/evals]]); report NDCG@10 delta. Target: +5% or better.

## Done signal

- TEI reranker running, passing health checks.
- `mode=deep` requests produce expected shape with `rerank_score` populated.
- Shadow eval shows improvement on > 70% of queries.
- Rerank latency p95 ≤ 200ms for 20-pair batches on GPU.

## Rollback

Feature flag `MUSUBI_RERANK_ENABLED=false` forces `mode=deep` requests to fall through to `mode=fast` ordering. No data change to undo.

## Smoke test

```
> retrieve (mode=deep): "how do I restart livekit workers"
# Top result should be more specific than the mode=fast version on the same query.
```

## Estimate

~3 days. Reranker is a drop-in.

## Pitfalls

- **Pair batching.** Naive per-doc rerank calls are slow. Always batch (40+ pairs at once) — rerank is compute-dense.
- **Token limits.** Rerank prompts combine query + doc; long docs truncate. For long chunks, rerank on a compact summary rather than the whole body.
- **Calibration.** Cross-encoder scores aren't probabilities; we use them ordinally (ranking) + normalized for the relevance-component blend.
