---
title: Re-embedding
section: 11-migration
tags: [embeddings, migration, named-vectors, section/migration, status/complete, type/migration-phase]
type: migration-phase
status: complete
updated: 2026-04-17
up: "[[11-migration/index]]"
reviewed: false
---
# Re-embedding

How Musubi handles embedding-model changes without downtime or data loss.

## Why re-embed

- Release a better model (e.g., BGE-M3 → BGE-M4 in a year).
- Fix a bug in the embedding pipeline (e.g., wrong pooling, wrong truncation).
- Move from Gemini (cloud) to BGE-M3 (local) or vice versa.

## The discipline: named vectors

Every collection stores vectors under named keys (`dense_bge_m3_v1`, `sparse_splade_v1`, etc.). Adding a new model adds a new named vector; it doesn't overwrite the old.

This means:

- Multiple models can coexist during migration.
- Retrieval chooses which one to use via config.
- Rollback is "switch the config back" — no data lost.

## Re-embed procedure

### Step 1: Add the new named vector

```python
client.update_collection(
    "musubi_episodic",
    vectors_config={
        "dense_bge_m4_v1": VectorParamsDiff(
            size=1024,  # or whatever the new model dim is
            distance=Distance.COSINE,
            quantization_config=ScalarQuantization(...),
        ),
    },
)
```

Existing points have no value for the new key (null). HNSW index is empty for the new vector.

### Step 2: Backfill

```
musubi-cli re-embed \
  --collection musubi_episodic \
  --source dense_bge_m3_v1 \
  --target dense_bge_m4_v1 \
  --batch-size 64
```

Worker:

1. Scrolls the collection in batches.
2. For each point, fetches content.
3. Encodes with the new model.
4. `update_vectors` on the point with the new named vector.
5. Tracks progress via `backfill_cursor` in `lifecycle-work.sqlite`.

Resumable. Takes ~N minutes where N ≈ points / (batch × throughput).

### Step 3: Dual-read

Config:

```env
RETRIEVE_DENSE_VECTOR=dense_bge_m3_v1    # old
SHADOW_DENSE_VECTOR=dense_bge_m4_v1      # new
```

Retrieval runs both; shadow result is logged + compared on a dashboard. Latency budget: old counts; shadow is fire-and-forget.

### Step 4: Promote

After 1-2 weeks of shadow:

```env
RETRIEVE_DENSE_VECTOR=dense_bge_m4_v1    # new is primary
SHADOW_DENSE_VECTOR=dense_bge_m3_v1      # old becomes shadow
```

Monitor for regressions; invert the config if anything breaks.

### Step 5: Retire

After 4 weeks of stable new-primary:

```python
client.delete_vector(
    "musubi_episodic",
    name="dense_bge_m3_v1",
)
```

Storage reclaimed. Old model's name is now free.

## What triggers retirement

Don't retire early. Keep the old vector until:

- Shadow diff is acceptable.
- No rollback windows remaining.
- Storage pressure justifies reclaim.

Typical lifecycle: 1 month warm + 3 months cold = 4 months before retirement.

## Across all collections

Re-embed one collection at a time, or run parallel workers per-collection. Either works; sequential is safer, parallel is faster.

Collections' named vector names can differ — `musubi_concept` can keep `dense_bge_m3_v1` while `musubi_episodic` moved to v4. Use a `model-version-map.yaml` to track which vector each collection queries against.

## Sparse vector changes

Same procedure, different config. SPLADE++ V3 → V4 would add `sparse_splade_v2`, backfill, shadow, promote, retire.

## Changing dimensions

BGE-M3 → something with a different dimension (say, 768 → 1024) is fine — named vectors are independent. Quantization config too.

## Reranker swap

Reranker doesn't store vectors; it runs at query time. Swapping the reranker is "deploy new container, flip config" — no data change. See [[11-migration/phase-3-reranker]] for the pattern.

## LLM swap

LLM is used offline for synthesis/rendering; no storage implications. Swap by updating `.env` + running the next synthesis job. Outputs differ but that's expected.

## Embedding parity tests

Before promoting, run:

- **Parity on golden set:** old and new must return similar top-5 for > 70% of queries. Drift > 30% flags a problem.
- **Nearest-neighbor stability:** for a sample of 100 memories, the 5 nearest-neighbor memories under old vs new should overlap > 60%. Low overlap means the new model encodes different structure — might be desired (better semantics) or broken.

## Failure modes

**Backfill stuck halfway.** Cursor + idempotent upsert → safe to restart.

**New model OOMs under batch load.** Reduce batch size in TEI config; re-encode is idempotent.

**New model performance worse on shadow.** Investigate; likely query-specific. Don't promote; retire the new vector.

## Automation

Long-term: `musubi-cli re-embed --auto` — reads `model-version-map.yaml`, finds model versions lagging the latest, backfills automatically. Post-v1.

## Test contract

**Module under test:** `musubi-cli re-embed` + core's update_vectors path

1. `test_new_named_vector_added_without_touching_old`
2. `test_backfill_cursor_resumes_on_restart`
3. `test_retrieval_config_switch_no_data_change`
4. `test_dual_read_shadow_logs_diff`
5. `test_retire_vector_reclaims_storage`
6. `test_parity_on_golden_set_ge_70_percent`
7. `test_nn_overlap_on_sample_ge_60_percent`
