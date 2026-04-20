---
title: "Slice: Qdrant layout + indexes"
slice_id: slice-qdrant-layout
section: _slices
type: slice
status: done
owner: eric
phase: "1 Schema"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-18
reviewed: true
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-poc-data-migration]]", "[[_slices/slice-retrieval-hybrid]]"]
---
# Slice: Qdrant layout + indexes

> `ensure_collections()` + named-vector configuration + payload indexes. Idempotent on boot.

**Phase:** 1 Schema · **Status:** `done` · **Owner:** `eric`

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

### 2026-04-18 — eric — closure (retrospective, under the new Closure Rule)

Flipping `status: in-progress → done`. This slice's first cut (commit `0f46281`) has been successfully consumed by slice-plane-episodic (which imports `store.collection_for_plane("episodic")`, `store.specs.DENSE_VECTOR_NAME`, and `store.specs.SPARSE_VECTOR_NAME` directly for its upsert + query paths). The in-memory Qdrant client tests in `tests/store/` + the live-behavior evidence from `tests/planes/test_episodic.py` (which uses the same `store` module to boot collections before each test) confirm the public surface works.

Applying the [Test Contract Closure Rule](../00-index/agent-guardrails.md#Test-Contract-Closure-Rule) retrospectively. Per the spec's `## Test contract` in [[04-data-model/qdrant-layout]]:

| Bullet | Closure state | Evidence |
|---|---|---|
| `test_ensure_collections_idempotent` | ✓ passing | `tests/store/test_collections.py::TestIdempotency::test_second_boot_creates_nothing` + `test_third_boot_still_creates_nothing` |
| `test_ensure_indexes_idempotent` | ✓ passing | `tests/store/test_indexes.py::TestIdempotency::test_second_boot_is_no_op_when_server_reports_existing_schema` + `test_already_exists_exception_treated_as_noop` |
| `test_adding_new_index_does_not_rebuild_collection` | ✓ passing | `tests/store/test_collections.py::TestPartialStateTolerance::test_existing_subset_is_not_re_created` |
| `test_quantization_applied_to_dense_vector` | ✓ passing | `tests/store/test_collections.py::TestVectorConfig::test_quantization_applied_to_dense` |
| `test_hybrid_search_returns_rrf_fused_scores` | ⊘ out-of-scope for this slice | Hybrid search is `slice-retrieval-hybrid`'s concern; this slice owns the *layout* that makes hybrid possible (named dense + sparse vectors configured). Explicit per Method-ownership rule. |
| `test_namespace_filter_required_on_every_query` | ⊘ deferred (lint-style) | A lint-style check belongs in a future `import-linter` / custom lint rule, not in this slice's pytest surface. Tracked as a lint TODO; enforced by review today. |
| `test_scroll_pagination_handles_large_collection` | ⊘ deferred (integration) | Pagination against a real Qdrant (not in-memory) belongs in `tests/integration/`. This slice doesn't own a query API to paginate. |
| `test_batch_update_points_preferred_over_loop` | ⊘ deferred (lint-style) | Same as #6 — belongs to a lint rule + reviewer enforcement. |
| `test_sparse_vector_full_scan_threshold_configurable` | ✓ passing | `tests/store/test_specs.py::TestRegistry::test_sparse_opt_in_per_collection` + `CollectionSpec.sparse_full_scan_threshold` field |
| `test_collection_names_come_from_config_only` | ✓ passing | `tests/store/test_names.py` (4 tests asserting every consumer reaches through `store.names.collection_for_plane` / `COLLECTION_NAMES`) |

Property tests (11, 12 in the spec — `hypothesis: RRF-fusion stability`, `hypothesis: scroll yields each point exactly once`) deferred with the same rationale as bullets 5 and 7: they belong to the retrieval slice and to integration, respectively.

Integration tests (13, 14 in the spec) deferred entirely to `tests/integration/` — requires a dockerised Qdrant.

**Historical footnote:** same as slice-types — this landed as a direct commit to `v2` before branch protection + the Issue board. Retrospective closure is a one-time reconciliation; all future `done` transitions go through the PR + review lifecycle.

Closing the corresponding GitHub Issue via the Dual-update rule in the same commit that flips this frontmatter.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
