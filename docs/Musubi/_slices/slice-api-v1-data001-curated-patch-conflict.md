---
title: "Slice: DATA-001 one-shot curated HTTP PATCH conflicts"
slice_id: slice-api-v1-data001-curated-patch-conflict
issue: 637
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, api, data-integrity]
updated: 2026-08-03
reviewed: false
depends-on: []
blocks: []
---

# Slice: DATA-001 one-shot curated HTTP PATCH conflicts

Make the curated HTTP PATCH boundary honor the caller's observed version exactly
once. A stale caller receives typed 409 and is never silently rebased into a later
winner. Preserve the existing retrying `CuratedPlane.patch_metadata` contract for
internal callers that have nobody to decide whether a retry is still appropriate.

## Decision boundary

- HTTP PATCH uses the one-shot, layout-aware, non-re-embedding primitive shipped by #634.
- Internal plane PATCH continues to use `owned_update` and its bounded rebase/retry policy.
- The HTTP allowlist remains exactly `tags`, `importance`, and `topics`; tag replacement
  semantics do not become merge semantics.
- Namespace binding, v1/v2 identity-row selection, exact-token attribution, generation
  binding, and fail-closed dangling-pointer handling remain unchanged.

## Owned paths

- `src/musubi/api/routers/writes_curated.py`
- `tests/api/test_data001_curated_patch_conflict.py`
- `docs/Musubi/07-interfaces/canonical-api.md`
- `docs/Musubi/_slices/slice-api-v1-data001-curated-patch-conflict.md`
- `docs/Musubi/_inbox/locks/slice-api-v1-data001-curated-patch-conflict.lock`

## Forbidden paths

- `src/musubi/store/immutable_vectors.py` — reuse the reviewed one-shot primitive.
- `src/musubi/planes/curated/plane.py` — preserve the internal retrying seam.
- `tests/planes/test_curated.py` — existing internal/dangling-pointer proofs remain evidence.

## Test Contract

1. `test_curated_http_patch_same_version_loser_returns_typed_conflict_without_retry`
2. `test_curated_http_patch_preserves_replace_tag_topic_and_importance_semantics`
3. `test_curated_http_patch_v2_changes_only_anchor_and_preserves_generation_binding`
4. `test_patch_metadata_preserves_concurrent_state_access_bumps_version_once`
5. `test_patch_curated_router_refuses_dangling_pointer_without_mutation`

## Work log

- 2026-08-03 — `codex-gpt5` claimed Issue #637 after #634 established that HTTP
  and internal mutations need deliberately different concurrency contracts. Tests
  are written first; the stale-observation case injects a real winner after route
  validation so the current retrying route must fail by replaying the loser.
