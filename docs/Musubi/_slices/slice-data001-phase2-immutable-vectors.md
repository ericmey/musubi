---
owner: aoi
status: in-progress
issue: 530
title: "Slice: DATA-001 Phase 2 — immutable vectors + fenced committed pointer"
slice_id: slice-data001-phase2-immutable-vectors
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: [slice-data001-concurrent-full-object-update]
blocks: []
---
# Slice: DATA-001 Phase 2 — immutable vectors + fenced committed pointer

## Context

Phase 1 (PR #539) made payload-only updates concurrency-safe via the attributable
`update_lease_token` mutation lease, but the deployed Qdrant (server 1.15) **silently ignores**
`update_vectors`' `update_filter` (proven on real Qdrant 6339). A vector write cannot be
token-fenced, and a full-point `upsert` clobbers RET-008 `access_count`. Phase 1 therefore left the
two vector-changing paths — `EpisodicPlane._reinforce` when NEW content wins, and `CuratedPlane`
same-id body update — on best-effort behavior. Phase 2 closes that with immutable content points +
a fenced anchor pointer swap. Design: `docs/Musubi/13-decisions/data001-phase2-immutable-vectors.md`.

## Invariant (Yua, 2026-07-15)

A vector-changing update publishes a NEW immutable content point and commits by a SINGLE fenced
`set_payload` on a stable `object_id`-keyed anchor that swaps `live_point` + narrow payload +
`version+1` + `done`. Losers can never change a visible vector; reads expose only the content point
named by the committed `live_point`; crash/retry converges by replay from disk; RET-008 lease-owned
access fields and Phase-1 narrow payload mutation stay intact.

## Mechanism (mirrors the proven ART-001 head/generation/fenced-publish pattern)

- Stable **anchor** point keyed by `object_id`, carrying `vector_layout_version`, `live_point`,
  `version`, the lease + access fields. Zero/non-search vector + anchor-kind marker so it never ranks.
- **Content points** are write-once; `content_point_id`/generation derive from the stable
  `operation_key` (not the per-claim `owner_token`).
- The COMPLETE mutation (content + narrow fields + vector source) is persisted in the coordinator
  outbox before staging → replay-from-disk with no caller memory.
- Handler `immutable_vector_publish` registered on the coordinator via `register_intent_handler`
  (ART-001 precedent); the coordinator owns admission/claim/reconcile/terminal. Cleanup is terminal
  correctness (retry, never confirmed-on-best-effort).
- `vector_layout_version`: v1 legacy row served as self-pointer; v2 anchor must have `live_point` or
  fails closed; first vector mutation bootstraps v1 → content point + v2 anchor.

## owns_paths

- `src/musubi/store/immutable_vectors.py`
- `tests/store/test_data001_phase2_immutable_vectors.py`

## Test Contract (11, tests-first, real Qdrant; RED before GREEN)

1. `test_old_owner_late_write_never_becomes_visible` — RED
2. `test_content_point_id_is_stable_across_reconcile` — RED
3. `test_crash_before_pointer_replays_from_disk` — RED
4. `test_crash_after_pointer_no_double_apply` — RED
5. `test_cleanup_failure_returns_retry_pointer_stays_attributable` — RED
6. `test_concurrent_access_lease_composition` — RED
7. `test_no_future_mutation_orphan_reconciled` — RED
8. `test_read_follows_committed_pointer_only` — RED
9. `test_anchor_never_ranks_in_vector_search` — RED
10. `test_legacy_v1_served_as_self_pointer_and_v2_missing_pointer_fails_closed` — RED
11. `test_first_vector_mutation_bootstraps_v1_to_v2` — RED

## Out of scope / follow-up

- Wiring `_reinforce` (new-content-wins) and `CuratedPlane` body-update call sites onto the new
  handler is the integration step after the store-level contract is GREEN; it composes with the
  Phase-1 mutation lease (payload) unchanged.
