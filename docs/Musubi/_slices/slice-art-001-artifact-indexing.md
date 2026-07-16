---
title: "Slice: ART-001 committed-generation artifact indexing"
slice_id: slice-art-001-artifact-indexing
issue: 451
section: _slices
type: slice
status: done
owner: claude-code-opus48
phase: "4 Planes"
tags: [section/slices, status/done, type/slice]
updated: 2026-07-15
reviewed: true
depends-on: ["[[_slices/slice-plane-artifact]]"]
blocks: []
---
# Slice: ART-001 committed-generation artifact indexing

> Fix the artifact-plane reindex bug (#451): a single committed generation per artifact, staged under a
> never-reused `(generation, owner)`, published by a fenced head replace + exact readback, with
> fail-closed head-first reads and an additive lifecycle intent-kind driving upload → indexed → retrieve.
> Implements [[13-decisions/0036-artifact-committed-generation-indexing]].

**Phase:** 4 Planes · **Status:** `in-review` · **Owner:** `claude-code-opus48`

## Specs to implement

- [[13-decisions/0036-artifact-committed-generation-indexing]]

## Owned paths

- `src/musubi/planes/artifact/indexer.py`
- `tests/planes/test_artifact_indexing.py`
- `tests/planes/test_artifact_indexing_integration.py`
- `tests/api/test_artifact_upload_indexing.py`
- `docs/Musubi/13-decisions/0036-artifact-committed-generation-indexing.md`
- `docs/Musubi/_slices/slice-art-001-artifact-indexing.md`

(The fix additively touches shared files owned by other slices — `src/musubi/planes/artifact/plane.py`,
`src/musubi/lifecycle/{coordinator,store,runner}.py`, `src/musubi/types/artifact.py`,
`src/musubi/api/routers/writes_artifact.py`. Each change is additive + backward-safe; see Work log.)

## Test Contract

1. `test_c4_upload_to_index_to_retrieve_single_committed_generation`
2. `test_reindex_from_more_to_fewer_hides_old_tail`
3. `test_first_ever_index_failure_exposes_zero_chunks`
4. `test_reindex_failure_keeps_previous_generation_visible`
5. `test_legacy_indexed_head_deserializes_and_fails_closed_then_reindexes`
6. `test_query_with_degradation_warns_generation_churn_when_budget_saturated`
7. `test_query_with_degradation_no_warning_when_genuinely_sparse`
8. `test_async_index_empty_content_fails_closed`
9. `test_publish_failed_fences_on_stale_publication_version`
10. `test_async_index_invalid_utf8_fails_closed`
11. `test_async_index_unknown_chunker_fails_closed`
12. `test_async_index_transient_embed_failure_retries_not_failed`
13. `test_async_reindex_reclaims_only_prior_generation`
14. `test_enqueue_at_capacity_marks_head_failed_visible_terminal`
15. `test_upload_202_state_is_indexing_axis_and_get_truth`
16. `test_upload_at_capacity_202_state_failed_and_get_truth`
17. `integration: test_concurrent_same_artifact_index_single_committed_generation` (real Qdrant, inv #5)

## Work log

### 2026-07-15 — claude-code-opus48 (aoi): C4/ART-001 production implementation (Issue #451)

Implements ADR 0036 (validated proposal). One production branch/PR carrying ADR + slice; additive
intent-kind on the lifecycle worker/outbox, not a new engine.

- **Additive data contract** — `SourceArtifact` += `committed_generation`/`committed_owner`/
  `index_operation_id`/`publication_version`; `ArtifactChunk` += `generation`/`owner_token`. Optional,
  backward-deserialization-safe; the `indexed⇒committed_generation` validator is deliberately NOT
  hard-enforced (legacy indexed heads must still load) — enforcement lives on the publish path + the
  fail-closed reads.
- **Coordinator additive dispatch** — `intent_kind` column (backward-safe ADD-COLUMN migration),
  `register_intent_handler`, `enqueue_index_intent` (graceful backpressure), and a single branch in
  `_reconcile_claimed` delegating to `_drive_custom_intent`/`_finalize_custom`. The lifecycle-transition
  path is untouched (s1–s4 green).
- **`ArtifactIndexer`** (async durable path via `reconcile_once`) + committed **`index()`** (sync path):
  stage `(generation, owner)`-tagged chunks, `publication_version`-fenced head publish, exact readback
  as the sole win signal, generation-scoped loser/GC cleanup. Failure policy: first-fail → fail-closed;
  re-index-fail → previous-good stays visible; `_publish_failed` reads back before confirming.
- **Fail-closed head-first reads** (`query`, `query_by_artifact`, `chunks_for`) + `query_with_degradation`
  (bounded partial + `generation_churn`) — the C2 read-filter discipline.
- **Upload wiring** — `upload_artifact` enqueues the durable indexing intent; the indexer is registered
  in the `LifecycleRunner` startup.

Gates: `make check` (ruff + mypy strict + pytest + coverage), `make agent-check`, `make tc-coverage
SLICE=slice-art-001-artifact-indexing`; 10 contract/invariant tests (9 unit + 1 real-Qdrant
integration). No merge — awaiting independent exact-head review by Tama/Shiori (C2 read-filter is the
highest-risk focus).
