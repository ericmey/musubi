---
title: Qdrant Layout
section: 04-data-model
tags: [data-model, indexes, qdrant, section/data-model, status/complete, type/spec, vectors]
type: spec
status: complete
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
---
# Qdrant Layout

Exhaustive reference for the Qdrant schema: collections, named vectors, payload indexes, and parameters. This is the single place to look when asking "what fields can I filter on?" or "what vectors are available?"

## Qdrant version

**Minimum: Qdrant 1.15** (for stable named-vector hybrid search + server-side fusion + zero-vector points). See [[13-decisions/0005-hybrid-search]].

## Collections

| Collection | Purpose | Points per object | Vectors |
|---|---|---|---|
| `musubi_episodic` | Episodic memories | 1 | dense + sparse |
| `musubi_curated` | Curated knowledge index (derived from vault) | 1 | dense + sparse |
| `musubi_concept` | Synthesized concepts | 1 | dense + sparse |
| `musubi_artifact_chunks` | Chunks from source artifacts | N per artifact | dense + sparse |
| `musubi_artifact` | Artifact metadata (title + summary only) | 1 | dense (title+summary) |
| `musubi_thought` | Inter-presence messages | 1 | dense (optional; deferred under load) |
| `musubi_lifecycle_events` | Audit log mirror (optional) | 1 per event | dense (event reason) |

All collections share the same named-vector scheme (dense + sparse) with identical dimensions, so the retrieval code path is uniform.

## Named vectors

```python
# Per collection create params

vectors_config = {
    "dense_bge_m3_v1": VectorParams(
        size=1024,
        distance=Distance.COSINE,
        on_disk=False,              # keep hot; 32GB host has room
        hnsw_config=HnswConfigDiff(
            m=32,                   # slightly higher than default for recall
            ef_construct=256,
        ),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(
                type=ScalarType.INT8,
                quantile=0.99,
                always_ram=True,
            )
        ),
    ),
}

sparse_vectors_config = {
    "sparse_splade_v1": SparseVectorParams(
        index=SparseIndexParams(on_disk=False, full_scan_threshold=5000)
    ),
}
```

Rationale:

- **Named vectors** (not unnamed) so we can add `dense_bge_m3_v2` alongside the v1 without a collection rebuild when models change. See [[13-decisions/0006-pluggable-embeddings]].
- **INT8 scalar quantization** cuts RAM ~4x with ~1% recall loss at our corpus sizes; matters more when the index grows past ~1M points.
- **Sparse in-memory**: small footprint, big query speed win. We'll tune `full_scan_threshold` if small namespaces query poorly.

### Vector names in use

```
dense_bge_m3_v1        — BGE-M3 dense, 1024-d, cosine
sparse_splade_v1       — SPLADE++ V3, dictionary size = model vocab
dense_legacy_v0        — migration-only; old Gemini 3072-d (POC); removed post-phase-3
```

See [[11-migration/re-embedding]] for the migration plan.

## Payload schema (cross-cutting)

Every point has this base payload, serialized as the pydantic model's `model_dump(mode="json")`:

```json
{
  "object_id": "2W1eP3rZaLlQ4jTuYz0Q9CkZAB1",
  "namespace": "eric/claude-code/episodic",
  "schema_version": 1,
  "state": "matured",
  "created_at": "2026-04-17T09:00:00Z",
  "created_epoch": 1776249600.0,
  "updated_at": "2026-04-17T09:00:00Z",
  "updated_epoch": 1776249600.0,
  "version": 1,
  "tags": ["cuda", "nvidia"],
  "topics": ["infrastructure/gpu"],
  "importance": 7
}
```

Plus plane-specific fields defined in the individual docs.

## Payload indexes

Indexes are created idempotently at boot via `musubi/store/indexes.py`. The full set by collection:

### Universal (every collection)

| Field | Type | Reason |
|---|---|---|
| `namespace` | KEYWORD | Isolation — every query filters on namespace. |
| `object_id` | KEYWORD | Direct fetch. |
| `state` | KEYWORD | Lifecycle filters. |
| `schema_version` | INTEGER | Migration-aware reads. |
| `tags` | KEYWORD (array) | Tag filters. |
| `topics` | KEYWORD (array) | Topic filters. |
| `created_epoch` | FLOAT | Time ranges, recency sort. |
| `updated_epoch` | FLOAT | Same. |
| `importance` | INTEGER | Scoring + selection filters. |
| `version` | INTEGER | Optimistic-concurrency queries. |

### `musubi_episodic` (deltas)

| Field | Type | Reason |
|---|---|---|
| `content_type` | KEYWORD | Filter by capture type. |
| `capture_source` | KEYWORD | Provenance filters. |
| `capture_presence` | KEYWORD | Who captured it. |
| `access_count` | INTEGER | Reflection / demotion rules. |
| `reinforcement_count` | INTEGER | Promotion eligibility proxy. |
| `last_accessed_epoch` | FLOAT | Stale detection. |
| `supported_by.artifact_id` | KEYWORD (array) | Reverse-lookup from artifact. |
| `merged_into` | KEYWORD | Reverse-lookup from concept. |
| `superseded_by` | KEYWORD | Chain traversal. |

### `musubi_curated` (deltas)

| Field | Type | Reason |
|---|---|---|
| `vault_path` | KEYWORD | Lookup by file path. |
| `musubi_managed` | BOOL | Filter auto-managed vs human-only. |
| `valid_from_epoch` | FLOAT | Bitemporal filter. |
| `valid_until_epoch` | FLOAT | Bitemporal filter. |
| `promoted_from` | KEYWORD | Reverse-lookup from concept. |
| `supersedes` | KEYWORD (array) | Lineage. |
| `superseded_by` | KEYWORD | Chain head. |
| `body_hash` | KEYWORD | Echo detection. |
| `read_by` | KEYWORD (array) | Per-presence read state. |

### `musubi_concept` (deltas)

| Field | Type | Reason |
|---|---|---|
| `promoted_to` | KEYWORD | Reverse-lookup from curated. |
| `promotion_attempts` | INTEGER | Failure analytics. |
| `merged_from` | KEYWORD (array) | Which episodic fed this. |
| `merged_from_planes` | KEYWORD (array) | Source plane mix. |
| `contradicts` | KEYWORD (array) | Contradiction graph. |
| `last_reinforced_epoch` | FLOAT | Decay rule. |

### `musubi_artifact_chunks` (deltas)

| Field | Type | Reason |
|---|---|---|
| `artifact_id` | KEYWORD | Reverse-join to parent. |
| `chunk_id` | KEYWORD | Direct fetch. |
| `chunk_index` | INTEGER | Ordering. |
| `content_type` | KEYWORD | MIME filter. |
| `chunker` | KEYWORD | Chunker-type filter. |
| `source_system` | KEYWORD | Provenance. |

### `musubi_artifact` (metadata collection)

| Field | Type | Reason |
|---|---|---|
| `sha256` | KEYWORD | Deduplication. |
| `source_system` | KEYWORD | Provenance. |
| `source_ref` | KEYWORD | Back-ref to original. |
| `ingested_by` | KEYWORD | Auditing. |
| `artifact_state` | KEYWORD | `indexing` / `indexed` / `failed`. |
| `derived_from` | KEYWORD | Chain. |

### `musubi_thought` (deltas)

| Field | Type | Reason |
|---|---|---|
| `from_presence` | KEYWORD | Self-filter + "from whom" queries. |
| `to_presence` | KEYWORD | Inbox filter. |
| `channel` | KEYWORD | Channel routing. |
| `read` | BOOL | Unread-only filter. |
| `read_by` | KEYWORD (array) | Per-presence. |
| `in_reply_to` | KEYWORD | Thread walks. |

## Query patterns

### Standard hybrid search

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
    query_filter=models.Filter(
        must=[
            models.FieldCondition(key="namespace", match=models.MatchValue(value=ns)),
            models.FieldCondition(key="state", match=models.MatchAny(any=["matured"])),
        ]
    ),
    limit=20,
    with_payload=True,
)
```

Server-side RRF fusion is the default; we fall back to client-side only if we want custom weighting that RRF doesn't express. See [[05-retrieval/hybrid-search]].

### Batched payload mutations

**Always** use `batch_update_points` for multi-point updates:

```python
client.batch_update_points(
    collection_name="musubi_thought",
    update_operations=[
        models.SetPayloadOperation(
            set_payload=models.SetPayload(
                payload={"read_by": new_read_by},
                points=[pt.id],
            )
        )
        for pt in points
    ],
)
```

N+1 `set_payload` calls are a prohibited pattern (see [[00-index/agent-guardrails]]).

### Pagination

Aggregation queries must use `scroll` with `next_page_offset`:

```python
offset = None
while True:
    points, offset = client.scroll(
        collection_name=coll,
        scroll_filter=filter,
        limit=1000,
        with_payload=True,
        offset=offset,
    )
    for p in points:
        yield p
    if offset is None:
        break
```

## Resource budgets

Ballpark targets for the reference host (see [[03-system-design/process-topology]]):

- **`musubi_episodic`**: 100K points over 1 year of capture, ~1GB RAM (quantized), ~1GB disk.
- **`musubi_curated`**: 10K points, ~100MB.
- **`musubi_concept`**: 1K points, negligible.
- **`musubi_artifact_chunks`**: 1M chunks at peak (100K artifacts × ~10 chunks), ~10GB RAM quantized, ~40GB disk (chunked content stored as payload).
- **`musubi_artifact`**: 100K points (one per artifact), ~100MB.
- **`musubi_thought`**: 50K points, ~500MB.

If storage grows past disk budget, we partition by age (move chunks older than 1 year to on-disk-only collection) or move blobs to MinIO. See [[11-migration/scaling]].

## Multi-tenant future-proofing

Today: one Qdrant database, one tenant. Tomorrow: we have two options —

**A: Collection-per-tenant**: `musubi_episodic__eric` vs `musubi_episodic__other`. Ops overhead, but total isolation.

**B: Shared collections, namespace filter enforced in API**. Less overhead, but relies on filter correctness.

We lean toward **B** (shared collections), with per-tenant auth tokens at the API. Qdrant 1.15 supports strong namespace ACLs via payload filters, and our isolation tests catch mistakes. A future RBAC project can swap to A if a tenant's data needs to be physically separate (e.g., for compliance).

See [[10-security/auth]] and [[13-decisions/0008-no-relational-store]].

## Test contract

**Module under test:** `musubi/store/collections.py`, `musubi/store/indexes.py`

1. `test_ensure_collections_idempotent`
2. `test_ensure_indexes_idempotent`
3. `test_adding_new_index_does_not_rebuild_collection`
4. `test_quantization_applied_to_dense_vector`
5. `test_hybrid_search_returns_rrf_fused_scores`
6. `test_namespace_filter_required_on_every_query` (lint-style check)
7. `test_scroll_pagination_handles_large_collection`
8. `test_batch_update_points_preferred_over_loop` (lint-level check on imports)
9. `test_sparse_vector_full_scan_threshold_configurable`
10. `test_collection_names_come_from_config_only`

Property tests:

11. `hypothesis: for any query with same seeds + same corpus, RRF fusion result is stable`
12. `hypothesis: scroll over a collection yields each point exactly once`

Integration:

13. `integration: create collection, index, insert 1000 points, query with filter, assert recall ≥ 0.9 vs brute force`
14. `integration: boot sequence is idempotent — two boots produce identical collection schema`
