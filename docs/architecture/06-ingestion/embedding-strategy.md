---
title: Embedding Strategy
section: 06-ingestion
tags: [embeddings, indexing, ingestion, models, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Embedding Strategy

What we embed, when, with which model, and how we avoid unnecessary re-embedding. Answers the operational questions: "can we change models without rebuilding the index?" "what happens when BGE-M3 v2 ships?"

## Models

### Dense: BGE-M3

- Name: `BAAI/bge-m3`
- Dim: 1024
- Distance: cosine
- Context: 8K tokens
- Runtime: TEI on GPU, INT8 quantized, ~2.3GB VRAM
- Named vector: `dense_bge_m3_v1`

April 2026 choice. Strong on multilingual, short-query + long-passage dual-mode retrieval. Open weights. Pinned at a specific commit (tracked in `config.py` as `EMBEDDING_MODEL_REV`).

### Sparse: SPLADE++ V3

- Name: `naver/splade-v3` (or later naver revision)
- Output: sparse dict of term-weight pairs
- Runtime: TEI on GPU, ~700MB VRAM
- Named vector: `sparse_splade_v1`

April 2026 choice. Best open-weight sparse retriever for our corpus type.

### Reranker: BGE-reranker-v2-m3

- Name: `BAAI/bge-reranker-v2-m3`
- Runtime: TEI on GPU, ~2.3GB VRAM, co-loaded with BGE-M3 (shared tokenizer, no redundant load)
- Used in deep-path retrieval, not in ingestion

## What gets embedded

| Object | Dense target | Sparse target |
|---|---|---|
| EpisodicMemory | `title + "\n\n" + content[:2048]` | same |
| CuratedKnowledge | `title + "\n\n" + (summary or content[:2048])` | same |
| SynthesizedConcept | `title + "\n\n" + content` | same |
| SourceArtifact (metadata) | `title + "\n\n" + (summary or "")` | same |
| ArtifactChunk | `chunk_content` | same |
| Thought | `content` (only on async embed; see below) | — |

Truncation at 2048 chars keeps encode latency bounded. For episodic with longer `content`, we compute a 1-sentence summary (Ollama) during maturation and use `title + summary + content[:2048]` — not needed in hot-path capture.

## When we embed

### Capture (hot path)

- Required for: EpisodicMemory, SourceArtifact metadata.
- Parallel dense + sparse call to TEI.
- Cache query embeddings; **do not** cache content embeddings (they're written to Qdrant once).

### Maturation (background)

- Re-embed if content changed during supersession or deduplication merge.
- New summary (if computed) triggers re-embed for long content.

### Vault watcher

- Embed if new file, or `body_hash` changed.
- Embed if summary-length content changed; skip if only frontmatter meta changed.

### Synthesis

- New concept: embed `title + content`.
- Reinforced concept (no content change): **don't** re-embed. Just update `merged_from`, `reinforcement_count`, etc.

### Re-embedding migration

Model version change — see [[11-migration/re-embedding]]. Done in a dedicated migration job, not in the hot path.

## Batched encode

TEI supports batch input. For multi-item writes (batch capture, boot-scan re-embed), we batch up to 64 items per call. Throughput on RTX 3080:

- BGE-M3 dense: ~500 items/sec at batch 64.
- SPLADE++ sparse: ~300 items/sec at batch 64.

So re-embedding 100K points takes ~5 minutes of GPU time — acceptable for migrations, disruptive enough that we never re-embed on the hot path.

## Query-time encoding

Queries are short (1-50 tokens typically). Per-query encode time warm:

- Dense: 20ms p50, 45ms p95.
- Sparse: 25ms p50, 55ms p95.
- Parallel: bound by slower, ~30ms p50, 60ms p95.

In-memory LRU cache (10K entries) short-circuits repeat queries. Cache is keyed on raw query text; invalidated only at model swap.

## Named-vector discipline

Every write specifies the named vector explicitly:

```python
client.upsert(
    collection_name="musubi_episodic",
    points=[
        PointStruct(
            id=object_id,
            vector={
                "dense_bge_m3_v1": dense_vec,
                "sparse_splade_v1": sparse_vec,
            },
            payload=payload,
        )
    ],
)
```

Queries also specify `using=`:

```python
Prefetch(query=qv, using="dense_bge_m3_v1", limit=50)
```

This discipline is what lets us add a `dense_bge_m3_v2` alongside `v1` without touching existing code — existing writers/readers see only `v1`; migration scripts dual-write `v1 + v2`; cutover flips the `using=` clause to `v2`.

See [[13-decisions/0006-pluggable-embeddings]].

## When a model is deprecated

Lifecycle for an embedding model:

1. **Adopted**: added as a named vector, used in new writes + queries.
2. **Primary**: all hot paths use it.
3. **Secondary**: old writes dual-exist with new ones; new queries use the new model; old queries can opt in to the old via `using=`.
4. **Deprecated**: no new writes to the old named vector; queries fall back to new model.
5. **Removed**: named vector dropped from collection. Permanent; requires re-embedding anything we want to keep.

Re-embedding cutovers typically take ~2 weeks end-to-end to let old caches expire and evals confirm quality parity. See [[11-migration/re-embedding]].

## Model selection for future changes

Guardrails for swapping embedding models:

1. **Open weights** — we won't lock into Gemini or OpenAI again. If we want a hosted model for quality, we mirror it (keeps us portable).
2. **Dim ≤ 1024** — tests for VRAM + storage budgets are calibrated for this. Larger dims (3072 Gemini) bloat RAM / disk 3x.
3. **Bundled sparse** — if a new dense model also emits good sparse vectors (like BGE-M3 itself), we may consolidate to one model and free VRAM.
4. **Eval parity** — the new model must match or exceed current NDCG@10 on our golden set.

## Test contract

**Module under test:** `musubi/embedding/` (client + cache + batching)

Happy path:

1. `test_encode_dense_returns_1024_dim`
2. `test_encode_sparse_returns_nonempty_dict`
3. `test_encode_parallel_dense_sparse`
4. `test_batch_encode_64_items_one_call` (instrumented)
5. `test_truncate_content_to_2048_chars`

Cache:

6. `test_query_cache_hit_on_repeat`
7. `test_query_cache_miss_on_different_query`
8. `test_query_cache_cleared_on_model_revision_change`

Named vectors:

9. `test_upsert_specifies_both_named_vectors`
10. `test_query_uses_specified_named_vector`
11. `test_collection_can_add_new_named_vector_without_rebuild` (migration test)

Re-embedding:

12. `test_body_hash_unchanged_skips_reembed`
13. `test_body_hash_changed_triggers_reembed`
14. `test_synthesis_reinforce_does_not_reembed`

Degradation:

15. `test_tei_down_capture_returns_503`
16. `test_tei_timeout_on_batch_falls_back_to_sequential`

Property:

17. `hypothesis: for any content, encode(content) is stable across repeats (same weights)`

Integration:

18. `integration: full re-embedding job — old named vector read + new named vector write, dual-exist, cutover flip, evals stable`
19. `integration: boot scan with re-embedding — 10K files embedded in < 5 minutes on reference GPU`
