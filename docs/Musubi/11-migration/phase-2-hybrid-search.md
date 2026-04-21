---
title: "Phase 2: Hybrid Search"
section: 11-migration
tags: [bge-m3, hybrid-search, migration, phase-2, section/migration, sparse, status/stub, type/migration-phase]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-1-schema]]"
next: "[[11-migration/phase-3-reranker]]"
reviewed: false
---
# Phase 2: Hybrid Search

Introduce named vectors, swap the embedding model to BGE-M3, add SPLADE++ V3 sparse vectors, enable server-side RRF fusion.

## Goal

Retrieval quality improves materially and query path is forward-compatible with reranker / concepts / multi-plane. The old single-vector collection becomes one named vector within a multi-vector schema.

## What changes

### Services

- Stand up TEI containers for BGE-M3 (dense) and SPLADE++ V3 (sparse).
- Keep Gemini embedding path available as a fallback (`EMBEDDING_PROVIDER=gemini`).

### Collection schema

Create a **new** collection `musubi_episodic_v2` (don't mutate the live one):

```python
client.create_collection(
    "musubi_episodic_v2",
    vectors_config={
        "dense_bge_m3_v1": VectorParams(size=1024, distance=Distance.COSINE,
            quantization_config=ScalarQuantization(...)),
    },
    sparse_vectors_config={
        "sparse_splade_v1": SparseVectorParams(),
    },
)
# Payload indexes per [[08-deployment/qdrant-config]]
```

### Dual-write

From the moment `musubi_episodic_v2` exists, every new capture writes to **both** the old collection (with the old dense vector only) and the new one (with both named vectors).

Dual-write is the safety net. If the new path is buggy, the old collection is still complete.

### Backfill

Background job encodes every historical memory into the new collection:

```
musubi-cli backfill --collection musubi_episodic_v2 --batch-size 64
```

Processes in batches. Skips points already backfilled (tracked via a `backfill_cursor` in `lifecycle-work.sqlite`).

At POC-era scale (few thousand memories), backfill completes in < 1 hour.

### Retrieve path

Dual-read during cutover:

- Fast path uses `musubi_episodic_v2` (hybrid).
- A shadow query runs against the old collection (dense-only).
- Both results get logged; diff published on a dashboard.

After 1 week of shadow with no regression, the old collection stops being queried; after 2 more weeks, it's retired.

### RRF fusion

Use Qdrant's `FusionQuery`:

```python
client.query_points(
    "musubi_episodic_v2",
    prefetch=[
        Prefetch(query=dense_vec, using="dense_bge_m3_v1", limit=50),
        Prefetch(query=sparse_vec, using="sparse_splade_v1", limit=50),
    ],
    query=FusionQuery(fusion=Fusion.RRF),
    limit=k,
)
```

Server-side fusion is faster and less error-prone than client-side merge.

## Done signal

- New captures visible in both collections.
- Backfill completes; counts match (`old.count == new.count`).
- Shadow diff shows < 10% result reshuffle on benchmark queries.
- Retrieve p50 ≤ 150ms (local TEI on GPU).
- Gemini-based path still works if `EMBEDDING_PROVIDER=gemini`.

## Rollback

If new path is broken:

- Route retrieval back to the old collection (feature flag `RETRIEVE_COLLECTION=musubi`).
- Dual-write remains; data in new collection is preserved, just not queried.
- Fix forward and re-enable.

Config flag location: `/etc/musubi/.env` (operator-level switch).

## Smoke test

```python
# pytest tests/test_hybrid.py
def test_hybrid_beats_dense_on_rare_term():
    # rare-term query "alpaca-bleu"
    dense_only = retrieve(mode="dense_only", query_text="alpaca-bleu")
    hybrid = retrieve(mode="fast", query_text="alpaca-bleu")
    assert hybrid.top_score > dense_only.top_score * 0.8  # usually >
```

```
# Real-world:
> recall: "that thing with the CUDA 13 driver"
# Verify the expected memory surfaces.
```

## Estimate

~2 weeks. TEI bring-up, named-vector rewrite of store/query functions, backfill worker, shadow eval infra.

## Pitfalls

- **Don't forget payload.** Dual-write must replicate payload into the new collection; check indexes too.
- **SPLADE++ output format.** V3 output is dict of `{term: weight}` tuned slightly differently from V2; verify the Qdrant sparse payload shape matches.
- **Backfill reads lag.** New captures during backfill must be tracked separately; otherwise you'd double-encode. The cursor handles this — backfill uses `created_epoch < cursor_epoch`.
- **Embedding dimension change.** BGE-M3 is 1024; Gemini-001 was 3072. Can't put both into the same named vector — named vectors are the whole point.
