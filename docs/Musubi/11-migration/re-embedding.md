---
title: Re-embedding
section: 11-migration
tags: [embeddings, migration, named-vectors, section/migration, status/draft, type/migration-phase]
type: migration-phase
status: draft
updated: 2026-10-01
up: "`index`"
reviewed: false
---
# Re-embedding

> **Planned, tooling not implemented.** This page is the design for changing embedding models. None of the re-embed tooling below exists yet: there is no re-embed command, no backfill worker, no shadow read, and no setting that selects which named vector retrieval uses. Treat every procedure here as a plan.

How Musubi should handle embedding-model changes without downtime or data loss.

## Why re-embed

- Release a better model (e.g., BGE-M3 → a successor).
- Fix a bug in the embedding pipeline (e.g., wrong pooling, wrong truncation).
- Move between a local TEI endpoint and a remote embeddings endpoint.

## What exists today

- **Named vectors.** Every collection stores vectors under named keys. The names and the dense size are constants, not settings: `DENSE_VECTOR_NAME = "dense_bge_m3_v1"`, `SPARSE_VECTOR_NAME = "sparse_splade_v1"`, `DENSE_SIZE = 1024` (`src/musubi/store/specs.py:22-24`). Retrieval always queries those names.
- **Embedding endpoints are settings.** `TEI_DENSE_URL`, `TEI_SPARSE_URL`, `TEI_RERANKER_URL`, `EMBEDDING_MODEL`, `SPARSE_MODEL` (`src/musubi/settings.py:67-81`). Pointing them at a different model without re-embedding mixes two models' vectors under one name. Don't.
- **Vector writes go through the immutable-vector publisher.** The old in-place `update_vectors` path is retired. A vector change writes a new write-once content point and commits it with one fenced update of the object's anchor (`src/musubi/store/immutable_vectors.py`; wired in `src/musubi/lifecycle/runner.py:589-596`). Any backfill has to write through this seam, not around it.

## The discipline: named vectors

Adding a new model should add a new named vector, not overwrite the old one. This means:

- Multiple models can coexist during migration.
- Retrieval could choose which one to use via config (not implemented; the names are constants today).
- Rollback is "switch back" — no data lost.

## Re-embed procedure (planned)

### Step 1: Add the new named vector

Declare the new vector name alongside the old one in `src/musubi/store/specs.py`. Whether Qdrant can add a named vector to an existing collection in place, or whether this needs a new collection plus a copy, is unverified here; check the Qdrant version in use before relying on either.

### Step 2: Backfill

A planned command (shape not final):

```
musubi re-embed \
  --collection musubi_episodic \
  --source dense_bge_m3_v1 \
  --target dense_<new-model>_v1 \
  --batch-size 64
```

The worker would:

1. Scroll the collection in batches.
2. For each object, fetch the committed content.
3. Encode with the new model.
4. Publish the new named vector through the immutable-vector publisher.
5. Track progress in a resumable cursor.

### Step 3: Dual-read

A shadow mode would run retrieval against both vectors, log and compare the shadow result, and keep the old vector authoritative for latency and results.

### Step 4: Promote

After 1-2 weeks of shadow, make the new vector primary and keep the old one as the shadow. Monitor for regressions; swap back if anything breaks.

### Step 5: Retire

After about 4 weeks of stable new-primary, drop the old vector and reclaim storage.

## What triggers retirement

Don't retire early. Keep the old vector until:

- Shadow diff is acceptable.
- No rollback windows remain.
- Storage pressure justifies reclaim.

Typical lifecycle: 1 month warm + 3 months cold = 4 months before retirement.

## Across all collections

Re-embed one collection at a time, or run parallel workers per collection. Sequential is safer, parallel is faster.

Collections could then use different vector names (e.g. `musubi_concept` still on the old model while `musubi_episodic` has moved). Tracking that needs a per-collection model map; planned, not implemented.

## Sparse vector changes

Same procedure: a new SPLADE model would add `sparse_splade_v2`, backfill, shadow, promote, retire.

## Changing dimensions

A model with a different dimension needs its own named vector: you cannot put two dimensions into the same named vector, which is the whole point of naming them. Quantization config is per vector too.

## Reranker swap

The reranker doesn't store vectors; it runs at query time. Swapping it is "point `TEI_RERANKER_URL` at the new model and restart" — no data change.

## LLM swap

The LLM is used offline for maturation, synthesis and promotion rendering; no storage implications. Swap by updating `LLM_MODEL` / `OLLAMA_URL` in `.env` and restarting. Outputs differ, which is expected.

## Embedding parity tests

Before promoting, run:

- **Parity on golden set:** old and new must return similar top-5 for > 70% of queries. Drift > 30% flags a problem.
- **Nearest-neighbor stability:** for a sample of 100 memories, the 5 nearest-neighbor memories under old vs new should overlap > 60%. Low overlap means the new model encodes different structure — might be desired (better semantics) or broken.

## Pitfalls

- **SPLADE output format.** Sparse encoders differ in how they emit term weights; verify that the new model's output maps onto the Qdrant sparse vector shape (indices + values) before backfilling.
- **Embedding dimension change.** A different dimension cannot share a named vector with the old model; plan a new name, never an in-place swap.
- **Backfill reads lag.** New captures during a backfill must not be double-encoded; a cursor on `created_epoch` handles this.

## Failure modes

**Backfill stuck halfway.** Cursor + idempotent publish → safe to restart.

**New model OOMs under batch load.** Reduce the batch size in TEI config; re-encoding is idempotent.

**New model performance worse on shadow.** Investigate; likely query-specific. Don't promote; retire the new vector.

## Test Contract

**Module under test:** the planned re-embed command and the immutable-vector publish path. None of these tests exist yet.

1. `test_new_named_vector_added_without_touching_old`
2. `test_backfill_cursor_resumes_on_restart`
3. `test_retrieval_config_switch_no_data_change`
4. `test_dual_read_shadow_logs_diff`
5. `test_retire_vector_reclaims_storage`
6. `test_parity_on_golden_set_ge_70_percent`
7. `test_nn_overlap_on_sample_ge_60_percent`
