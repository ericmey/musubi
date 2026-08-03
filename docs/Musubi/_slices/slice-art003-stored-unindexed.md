---
title: "Slice: ART-003 truthful stored-unindexed artifacts"
slice_id: slice-art003-stored-unindexed
issue: 643
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "4-planes"
tags: [section/slices, status/in-progress, type/slice, artifacts, data-integrity]
updated: 2026-08-03
reviewed: false
depends-on: []
blocks: []
---

# Slice: ART-003 truthful stored-unindexed artifacts

Add the additive artifact indexing state required by ADR 0042: an artifact whose
blob is deliberately stored and readable by explicit id, but which owns zero
chunks and never claims indexing success, failure, or active admission.

## Decision boundary

- `stored_unindexed` is an honest indexing-axis state, not an alias for
  `indexing` or `failed`.
- It requires zero chunks and no committed generation, committed owner, indexing
  operation id, or failure reason.
- Its digest, size, title, and filename remain ordinary strict artifact metadata;
  the later ART-004 writer owns synthetic naming and exact blob persistence.
- Existing `indexing`, `indexed`, and `failed` behavior is unchanged.
- This slice proves by-id readability and zero committed chunks using a directly
  constructed fixture. ART-004 owns the production creation path and the semantic
  search miss with an indexed positive control.

## Specs to implement

- [[04-data-model/source-artifact]]
- [[04-data-model/object-hierarchy]]
- [[04-data-model/qdrant-layout]]
- [[13-decisions/0042-escrow-backed-episodic-retraction]]

## Owned paths

- `src/musubi/types/common.py`
- `src/musubi/types/artifact.py`
- `tests/types/test_artifact.py`
- `tests/planes/test_artifact.py`
- `openapi.yaml`
- `docs/Musubi/04-data-model/source-artifact.md`
- `docs/Musubi/04-data-model/object-hierarchy.md`
- `docs/Musubi/04-data-model/qdrant-layout.md`
- `docs/Musubi/_slices/slice-art003-stored-unindexed.md`
- `docs/Musubi/_inbox/locks/slice-art003-stored-unindexed.lock`

## Test contract

1. Strict validation accepts a canonical `stored_unindexed` artifact.
2. Every non-zero chunk or indexing-owned field makes that state invalid.
3. Failure reason is forbidden rather than interpreted as a failed state.
4. Existing indexing/indexed/failed fixtures and JSON round trips remain valid.
5. A directly created stored-unindexed head is readable by exact namespace/id and
   exposes zero committed chunks through both chunk-read surfaces.
6. `test_runtime_vs_snapshot_openapi_schema_parity` keeps the committed snapshot
   equal to the runtime schema, including the additive enum value.

## Work log

- 2026-08-03 — Claimed #643 after ADR 0042 merged at `d42201e`. Production
  escrow writing, blob durability, indexing-intent suppression, and semantic
  search proof remain explicitly owned by #644.
- 2026-08-03 — Tests written before production code. The first invocation was an
  invalid instrument because the fresh worktree lacked the dev extra and every
  async control failed before execution. After `uv sync --extra dev`, 42 existing
  controls passed and exactly the two new acceptance/readability cases failed on
  the absent enum; the five forbidden-field cases remain poised to fail if the
  enum lands without its invariant.
