---
title: "Slice: Qdrant layout + indexes"
slice_id: slice-qdrant-layout
section: _slices
type: slice
status: in-progress
owner: eric
phase: "1 Schema"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-retrieval-hybrid]]"]
---
# Slice: Qdrant layout + indexes

> `ensure_collections()` + named-vector configuration + payload indexes. Idempotent on boot.

**Phase:** 1 Schema · **Status:** `in-progress` · **Owner:** `eric`

## Specs to implement

- [[04-data-model/qdrant-layout]]
- [[08-deployment/qdrant-config]]

## Owned paths (you MAY write here)

  - `src/musubi/store/` — collection specs, index registry, `ensure_collections`, `ensure_indexes`, `bootstrap`

Path updated 2026-04-17 to `src/musubi/store/` — aligns the slice with the monorepo layout (ADR 0015) and the path the spec in `04-data-model/qdrant-layout#Test contract` calls out (`musubi/store/collections.py`, `musubi/store/indexes.py`). The original pre-monorepo paths (`musubi/collections.py`, `musubi/qdrant_bootstrap.py`) are retired.

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `src/musubi/planes/`
  - `src/musubi/retrieve/`
  - `src/musubi/types/` (owned by slice-types)

## Depends on

  - [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-plane-episodic]]
  - [[_slices/slice-plane-curated]]
  - [[_slices/slice-plane-artifact]]
  - [[_slices/slice-plane-concept]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-17 — eric — first cut landed on `v2` branch

- Implemented the full `src/musubi/store/` package: `names` (canonical collection names + plane → collection map), `specs` (CollectionSpec, IndexSpec, REGISTRY of 7 collections, UNIVERSAL_INDEXES + per-collection deltas), `collections.ensure_collections` (idempotent), `indexes.ensure_indexes` (idempotent, skips already-indexed fields + tolerates "already exists" responses), and `bootstrap.bootstrap` returning a frozen `BootstrapReport`.
- All seven collections per [[04-data-model/qdrant-layout#Collections]] are registered: `musubi_episodic`, `musubi_curated`, `musubi_concept`, `musubi_artifact`, `musubi_artifact_chunks`, `musubi_thought`, `musubi_lifecycle_events`. Each dense vector is 1024-d cosine BGE-M3 with INT8 scalar quantization + HNSW m=32, ef_construct=256. Sparse SPLADE++ V3 on 5/7 (`musubi_artifact` and `musubi_lifecycle_events` are dense-only per spec).
- Indexes are declared as a tuple of `IndexSpec`s — one universal set (namespace, object_id, state, schema_version, tags, topics, created_epoch, updated_epoch, importance, version) + per-collection deltas covering the full list in [[04-data-model/qdrant-layout#Payload indexes]].
- Idempotency strategy: `ensure_indexes` first queries `get_collection().payload_schema` and skips fields that already have an index (works against real Qdrant server); falls back to try/except around "already exists" errors (covers edge cases + double-boot semantics). In qdrant-client's local/in-memory mode, payload indexes are silent no-ops — mock-based tests verify call shape + skip-on-existing; a full integration test against a real Qdrant is deferred to `tests/integration/`.
- `qdrant-client>=1.12` added as a runtime dep (installed 1.17.1); `qdrant_client.*` added to mypy's ignore-missing-imports list. Pytest gained a `filterwarnings` entry suppressing the `"Payload indexes have no effect in the local Qdrant"` UserWarning.
- Slice `owns_paths` updated from the pre-monorepo `musubi/collections.py` + `musubi/qdrant_bootstrap.py` to the single directory `src/musubi/store/` — matches the spec's [[04-data-model/qdrant-layout#Test contract]] (which already named `musubi/store/collections.py`, `musubi/store/indexes.py`) and the monorepo layout from ADR 0015.
- Tests: 5 test modules under `tests/store/`, 46 tests covering names, spec registries, ensure_collections (including partial-state tolerance — a pre-existing collection is left alone), ensure_indexes (call shape, idempotency-by-schema-lookup, idempotency-by-exception-handling, `only=` filter), and bootstrap end-to-end. Test contract items covered: `ensure_collections_idempotent` ✓, `ensure_indexes_idempotent` ✓, `adding_new_index_does_not_rebuild_collection` ✓ (partial-state tolerance test), `quantization_applied_to_dense_vector` ✓, `sparse_vector_full_scan_threshold_configurable` ✓ (via CollectionSpec field), `collection_names_come_from_config_only` ✓. Deferred to later slices: `hybrid_search_returns_rrf_fused_scores`, `scroll_pagination_handles_large_collection`, `namespace_filter_required_on_every_query` (lint-style) → integration tests + slice-retrieval-hybrid.
- `make check` clean: ruff format + lint + `mypy --strict` on 36 files + 156/156 pytest (adds 46 to the 110 from slice-types).
- Commit on `v2`: see PR links below.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
