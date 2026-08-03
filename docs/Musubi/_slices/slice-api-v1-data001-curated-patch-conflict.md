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
- A physically versionless curated legacy row remains a supported theoretical input to
  the shared primitive, but the 2026-08-03 live raw-payload census found zero such
  patchable identity rows; version omission was confined to immutable content snapshots.

## Specs to implement

- [[07-interfaces/canonical-api]] — curated PATCH concurrency and replacement semantics.

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
- 2026-08-03 — Aoi found that the first red could accept the route's pre-existing
  dangling-pointer 409. The contract now asserts the observed-version-fence detail,
  so a wrong 409 cannot satisfy the concurrency proof.
- 2026-08-03 — Read-only raw Qdrant census archived at harem-ops commit `356f192`
  (`sha256:d6f1457f4ded16bf9f44d6fe3ad3a16fe4f0289b85afb475d693cebb7f1f466b`):
  121 physical curated points, complete pagination; all 104 patchable identity rows
  physically carry `version` (22 v1, 82 v2). The 17 version-absent points are all
  immutable `point_kind=content` snapshots. The API's 104-object distribution is an
  independent control on the physical classification, so the versionless-legacy case
  is documented rather than added as a live-population regression.
- 2026-08-03 — `make tc-coverage` caught that the slice named no normative spec even
  though it changed the canonical API contract. Added the explicit
  `[[07-interfaces/canonical-api]]` mapping; coverage now satisfies the closure rule.
