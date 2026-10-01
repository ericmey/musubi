---
title: Qdrant Layout
section: 04-data-model
tags: [data-model, indexes, qdrant, section/data-model, status/complete, type/spec, vectors]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: "src/musubi/store/"
---
# Qdrant Layout

Reference for the Qdrant schema: collections, named vectors, payload indexes, and parameters. The authority is `src/musubi/store/specs.py` (`REGISTRY` for collections, `UNIVERSAL_INDEXES` and `INDEXES_BY_COLLECTION` for payload indexes); this page reads it. This is the single place to look when asking "what fields can I filter on?" or "what vectors are available?"

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
| `musubi_thought` | Inter-presence messages | 1 | dense + sparse |
| `musubi_lifecycle_events` | Audit log mirror. Declared and created at boot, but not written yet (`src/musubi/lifecycle/events.py:13-14`) | 1 per event | dense only |

Every collection uses the same dense vector name and size. `musubi_artifact` and `musubi_lifecycle_events` are dense-only (`has_sparse=False`, `src/musubi/store/specs.py:104,108`); the rest also carry the sparse vector. Hybrid queries against a dense-only collection skip the sparse leg (`collection_has_sparse`, `specs.py:115-131`).

**Points per object.** Episodic and curated objects written through the immutable-vector path use a two-kind layout: one stable `anchor` point (the mutable identity row) plus one or more write-once `content` points, marked by the `point_kind` payload field (`specs.py:26-31`). Legacy single-point rows carry no `point_kind`. Read paths strip the layout-only fields before validating the model (`LAYOUT_ONLY_FIELDS`, `specs.py:38-48`).

## Named vectors

```python
# Per collection create params

vectors_config = {
    "dense_bge_m3_v1": VectorParams(
        size=1024,
        distance=Distance.COSINE,
        on_disk=False,              # keep vectors in RAM
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
```

See [[11-migration/re-embedding]] for the (planned) model-change procedure.

## Payload schema (cross-cutting)

Every point has this base payload, serialized as the pydantic model's `model_dump(mode="json")`:

```json
{
  "object_id": "2W1eP3rZaLlQ4jTuYz0Q9CkZAB1",
  "namespace": "alex/claude-code/episodic",
  "identity_family": "alex",
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

Indexes are created idempotently at boot by `ensure_indexes` in `src/musubi/store/indexes.py`, from the registries in `src/musubi/store/specs.py:138-242`. The full set by collection:

### Universal (every collection)

| Field | Type | Reason |
|---|---|---|
| `namespace` | KEYWORD | Isolation — every query filters on namespace. |
| `identity_family` | KEYWORD | Cross-presence filters on the first namespace segment. |
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
| `importance_last_scored_epoch` | FLOAT | Re-enrichment selection. |

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
| `artifact_state` | KEYWORD | `indexing` / `indexed` / `failed` / `stored_unindexed`. |
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

### `musubi_lifecycle_events` (deltas)

Declared for the mirror; the collection is empty until mirroring is wired.

| Field | Type | Reason |
|---|---|---|
| `event_id` | KEYWORD | Direct fetch. |
| `object_type` | KEYWORD | Filter by plane. |
| `from_state` | KEYWORD | Transition filters. |
| `to_state` | KEYWORD | Transition filters. |
| `actor` | KEYWORD | Who triggered it. |
| `occurred_epoch` | FLOAT | Time ranges. |
| `correlation_id` | KEYWORD | Join to a request. |

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

No age partitioning is implemented. If storage outgrows the disk budget, the options are moving old chunks to an on-disk-only collection or moving blobs to an object store. Both are planned, not implemented.

## Multi-tenant layout

Collections are shared: one `musubi_episodic` holds every namespace, and isolation is a `namespace` filter that the API applies on every query, backed by scoped tokens. A tenant is the first namespace segment (an agent such as `alex` or `sam`), not a separate database.

The alternative, collection-per-tenant (`musubi_episodic__alex`, `musubi_episodic__sam`), gives physical separation at the cost of ops overhead. It is not implemented; it remains the option if a tenant's data must be physically separate (e.g., for compliance).

See [[10-security/auth]] and [[13-decisions/0008-no-relational-store]].

## Test Contract

**Module under test:** `src/musubi/store/collections.py`, `src/musubi/store/indexes.py`, `src/musubi/store/specs.py`

The bullets below are the behaviour checklist. The implemented tests live in `tests/store/test_collections.py`, `tests/store/test_indexes.py` and `tests/store/test_specs.py` under their own names.

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
