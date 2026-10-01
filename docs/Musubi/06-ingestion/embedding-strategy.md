---
title: Embedding Strategy
section: 06-ingestion
tags: [embeddings, indexing, ingestion, models, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: "tests/test_embedding.py"
---
# Embedding Strategy

What Musubi embeds, when, with which model, and how it avoids unnecessary re-embedding. It answers the operational questions: "can we change models without rebuilding the index?" and "what happens when a new BGE-M3 ships?"

Code: `src/musubi/embedding/` (TEI clients, `ChunkedEmbedder`, caches) and `src/musubi/store/specs.py` (named vectors).

## Serving

All three models are served by Hugging Face **Text Embeddings Inference (TEI)**, reached over HTTP at `TEI_DENSE_URL`, `TEI_SPARSE_URL` and `TEI_RERANKER_URL`. They can run locally (the optional GPU overlay `deploy/docker/compose.local-gpu.yml` starts `tei-dense`, `tei-sparse` and `tei-reranker`) or on a remote inference host. Optional HTTP Basic auth applies to all three (`TEI_BASIC_AUTH_USERNAME` / `TEI_BASIC_AUTH_PASSWORD`).

`EMBEDDING_MODEL`, `SPARSE_MODEL` and `RERANKER_MODEL` record which model each endpoint serves. The model actually loaded is whatever the TEI server was started with.

## Models

### Dense: BGE-M3

- `BAAI/bge-m3`, 1024 dimensions, cosine distance, 8K-token context.
- Named vector: `dense_bge_m3_v1`.

Chosen for strong multilingual retrieval, good short-query/long-passage behaviour, and open weights.

### Sparse: SPLADE v3

- `naver/splade-v3` (TEI `--pooling splade`), a sparse term-weight vector.
- Named vector: `sparse_splade_v1`.
- Hard 512-token input cap (see "Long inputs").

### Reranker: BGE-reranker-v2-m3

- `BAAI/bge-reranker-v2-m3` (TEI `--pooling rerank`).
- Used in deep-path retrieval, not in ingestion.

## What gets embedded

Both vectors use the same text:

| Object | Embedded text |
|---|---|
| EpisodicMemory | `summary` if present, else `content` |
| CuratedKnowledge | `title + "\n\n" + (summary or content)` |
| SynthesizedConcept | `title + "\n\n" + synthesis_rationale` |
| SourceArtifact (metadata point) | none (a placeholder vector; the metadata point is not searched semantically) |
| ArtifactChunk | the chunk text |
| Thought | `content` (unless the caller defers embedding) |

Concepts embed title and rationale rather than the full content because that pairs the *what* with the *why* in two short fields, which carries more signal than an often padded LLM summary.

## Long inputs

- **Dense:** the client clips input at **32,000 characters** (`_DEFAULT_MAX_INPUT_CHARS_DENSE` in `src/musubi/embedding/tei.py`) and asks TEI to truncate to the model's token limit. 32,000 characters is a guard against pathological inputs, not a precise token bound.
- **Sparse:** SPLADE v3 accepts 512 tokens. `ChunkedEmbedder` (`src/musubi/embedding/chunked.py`) tokenizes each input with SPLADE's own tokenizer. Inputs over 510 tokens are split into sliding windows (64-token overlap), each window is embedded, and the per-window vectors are **max-pooled** into one. Max-pooling keeps "term X appears with weight ≥ W" for every window; averaging would dilute single-window terms. Core and the lifecycle worker both wrap their embedder in `ChunkedEmbedder`. A direct sparse client call without the wrapper is clipped at 2,048 characters.
- **Reranker:** clipped at 2,048 characters.

## When embedding happens

### Capture (hot path)

Episodic capture embeds dense and sparse before writing; the vector is required. Artifact upload embeds nothing on the hot path; chunks are embedded by the lifecycle worker's artifact indexer.

### Dedup merge

A merge keeps the vectors of whichever content won (`longer-wins`), so payload and vectors stay in sync. A `PATCH` of episodic `content` (used for retraction) does **not** re-embed: the retracted row stays findable by its original text.

### Vault sync

The watcher and the reconciler re-upsert a curated file only when its body hash changed. See [[06-ingestion/vault-sync]].

### Synthesis

A new concept is embedded once. Reinforcing an existing concept does not re-embed it.

### Re-embedding migration

A model change goes through a dedicated migration, not the hot path. See [[11-migration/re-embedding]].

## Batching

TEI accepts batches. The clients read the server's advertised `max_client_batch_size` from TEI `/info` and never exceed it. If `/info` is unavailable or malformed they fall back to a safe batch of 16; the static client default is 64. Throughput depends on the GPU and the TEI flags; measure it on your hardware (see [[09-operations/capacity]]).

## Query-time encoding

Queries are short. The fast path caches query embeddings in an in-process LRU (`QueryEmbeddingCache` in `src/musubi/retrieve/hybrid.py`, 10,000 entries), keyed on the raw query text and cleared when the model version changes. Content embeddings are never cached; they are written to Qdrant once.

The fast-path encoding budget is `RETRIEVAL_FAST_ENCODING_TIMEOUT_S` (default 0.25 s).

## Named-vector discipline

Every write names both vectors explicitly, using the constants in `src/musubi/store/specs.py`:

```python
models.PointStruct(
    id=point_id,
    payload=payload,
    vector={
        DENSE_VECTOR_NAME: dense_vec,    # "dense_bge_m3_v1"
        SPARSE_VECTOR_NAME: sparse_vec,  # "sparse_splade_v1"
    },
)
```

Queries also name the vector (`using=DENSE_VECTOR_NAME`). This is what lets a `dense_bge_m3_v2` be added alongside `v1` without touching existing code: existing readers and writers see only `v1`, a migration dual-writes `v1 + v2`, and the cutover changes the constant. See [[13-decisions/0006-pluggable-embeddings]].

## Retiring a model

The lifecycle of an embedding model:

1. **Adopted:** added as a named vector, used for new writes and queries.
2. **Primary:** all hot paths use it.
3. **Secondary:** old and new vectors coexist; new queries use the new model.
4. **Deprecated:** no new writes to the old vector.
5. **Removed:** the named vector is dropped from the collection. Anything worth keeping must be re-embedded first.

See [[11-migration/re-embedding]].

## Choosing a future model

Guardrails for swapping embedding models:

1. **Open weights,** so the index stays portable and self-hostable.
2. **Dimension ≤ 1024.** Storage and memory budgets assume it.
3. **Bundled sparse:** if a dense model also emits good sparse vectors (as BGE-M3 can), consolidating to one model frees GPU memory.
4. **Eval parity:** the new model must match or beat the current NDCG@10 on the golden set (see [[05-retrieval/evals]]).

## Test Contract

**Module under test:** `src/musubi/embedding/` (TEI clients, chunked embedder, cache, batching), `src/musubi/retrieve/hybrid.py` (query cache)

Happy path (`tests/test_embedding.py`):

1. `test_encode_dense_returns_1024_dim`
2. `test_encode_sparse_returns_nonempty_dict`
3. `test_encode_parallel_dense_sparse`
4. `test_batch_encode_64_items_one_call`
5. `test_batch_encode_above_64_chunks_requests`
6. `test_lower_deployed_tei_batch_ceiling_overrides_static_client_fallback`
7. `test_unavailable_or_malformed_tei_batch_contract_uses_safe_fallbacks`

Input limits:

8. `test_dense_client_truncates_to_default_safety_belt`
9. `test_dense_client_passes_long_realistic_content_untouched`
10. `test_sparse_client_keeps_pre_raise_safety_belt`
11. `test_dense_embed_request_sets_truncate_true`
12. sparse sliding-window chunking and max-pooling: `tests/test_chunked_embedder.py`

Cache:

13. `test_query_cache_hit_on_repeat`
14. `test_query_cache_miss_on_different_query`
15. `test_query_cache_cleared_on_model_revision_change`

Errors:

16. `test_tei_dense_client_raises_typed_error_on_5xx`
17. `test_tei_transient_5xx_retries_once`

Skipped (named-vector migration, re-embed, outage, latency and VRAM budgets):

18. `test_upsert_specifies_both_named_vectors`
19. `test_query_uses_specified_named_vector`
20. `test_collection_can_add_new_named_vector_without_rebuild`
21. `test_body_hash_unchanged_skips_reembed`
22. `test_body_hash_changed_triggers_reembed`
23. `test_synthesis_reinforce_does_not_reembed`
24. `test_tei_down_capture_returns_503`
25. `test_tei_timeout_on_batch_falls_back_to_sequential`
26. `test_tei_dense_encode_latency_p95_lt_50ms`
27. `test_tei_sparse_encode_latency_p95_lt_80ms`
