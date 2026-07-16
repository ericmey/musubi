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
updated: 2026-07-16
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

The store-level contract landed first; the coupled integration (Yua-approved 2026-07-15, see
"Scope" below) then legitimately extends into the identity consumers and the three write
compositions of the multi-point layout.

**Production:**

- `src/musubi/store/immutable_vectors.py` — the invariant core (anchor/content/dispatcher/projection)
- `src/musubi/store/specs.py` — `POINT_KIND_*`, `LAYOUT_ONLY_FIELDS`, `strip_layout_fields`
- `src/musubi/store/access_lease.py`, `src/musubi/store/mutation_lease.py` — `must_not content` (target the identity row)
- `src/musubi/store/raw_lookup.py` — `point_exists`/`raw_payload` target the identity row
- `src/musubi/lifecycle/coordinator.py` — `_read_object` excludes content; `enqueue_custom_intent`/`drive_intent`/`_claim(force_due)` seam
- `src/musubi/planes/episodic/plane.py` — reinforce publishes via the seam; `get()` anchor-aware; publisher injection
- `src/musubi/planes/curated/plane.py` — same-id update via the seam (author-frontmatter allowlist); `get()` anchor-aware; publisher injection
- `src/musubi/api/bootstrap.py`, `src/musubi/lifecycle/runner.py`, `src/musubi/vault/runtime.py` — the three write compositions register the dispatcher + inject publishers (runner: `register_boot_intent_handlers` before the boot reconcile)

**Tests (own + shared files extended for this integration):**

- `tests/store/test_data001_phase2_immutable_vectors.py` (own), `tests/lifecycle/test_custom_intent_seam.py` (own)
- Shared files extended: `tests/planes/test_episodic.py`, `tests/planes/test_curated.py`,
  `tests/planes/test_raw_lookup_and_delete.py`, `tests/lifecycle/test_c6b_atomicity.py`,
  `tests/api/test_bootstrap.py`, `tests/vault/test_vault003_live_delete.py` (fixtures wired +
  identity-consumer/composition discriminators added)

## Test Contract (exact test names; real Qdrant; each fix RED-proofed before GREEN)

`tests/store/test_data001_phase2_immutable_vectors.py` — invariant core (1–13):

1. `test_old_owner_late_write_never_becomes_visible`
2. `test_content_point_id_is_stable_across_reconcile`
3. `test_crash_before_pointer_replays_from_disk`
4. `test_crash_after_pointer_no_double_apply`
5. `test_cleanup_failure_returns_retry_pointer_stays_attributable`
6. `test_concurrent_access_lease_composition`
7. `test_no_future_mutation_orphan_reconciled`
8. `test_read_follows_committed_pointer_only`
9. `test_anchor_never_ranks_in_vector_search`
10. `test_legacy_v1_served_as_self_pointer_and_v2_missing_pointer_fails_closed`
11. `test_first_vector_mutation_bootstraps_v1_to_v2`
12. `test_publish_synchronous_committed_return_and_pending_raise`
13. `test_reinforce_rebases_on_fresh_unrelated_mutation_survives`

`tests/store/test_data001_phase2_immutable_vectors.py` — Yua path-audit correction discriminators:

14. `test_v1_bootstrap_in_place_fence_preserves_concurrent_mutation`
15. `test_payload_only_confirm_is_attributable_not_version_equality`
16. `test_publish_fails_loud_when_another_intent_active`
17. `test_cleanup_deletes_all_superseded_beyond_256`
18. `test_dangling_live_point_fails_closed`
19. `test_cross_object_live_point_fails_closed`
20. `test_v1_payload_only_metadata_mutation_commits_once`
21. `test_curated_projection_decides_vector_change`
22. `test_curated_content_change_without_summary_is_vector_change`
23. `test_episodic_reinforce_with_summary_is_projection_based`
24. `test_coordinator_read_object_excludes_content_shell`
25. `test_collection_aware_dispatch_routes_both_planes_and_rejects_unknown`
26. `test_cold_start_positive_registration_before_first_reconcile_commits`
27. `test_cold_start_negative_no_handler_first_reconcile_cannot_commit`

`tests/lifecycle/test_custom_intent_seam.py` — coordinator seam:

28. `test_artifact_index_wrapper_unchanged`
29. `test_generic_kind_round_trips_patch_through_fresh_coordinator`
30. `test_malformed_or_oversized_patch_fails_truthfully`
31. `test_cap_and_already_active_unchanged`
32. `test_drive_intent_touches_only_the_named_operation`
33. `test_drive_intent_bypasses_retry_backoff`

Identity-consumer + composition discriminators in extended shared files:

34. `tests/planes/test_raw_lookup_and_delete.py::test_presence_and_raw_payload_ignore_orphan_content_shell`
35. `tests/planes/test_episodic.py::test_get_returns_none_for_missing_id` (resolve-before-bump)
36. `tests/planes/test_episodic.py::test_concurrent_dedup_race_resolves_to_single_winner` (anchor-aware get)
37. `tests/planes/test_curated.py::test_same_id_update_inherits_state_lineage_access_from_fresh`
38. `tests/api/test_bootstrap.py::test_bootstrap_wires_write_planes_with_immutable_publisher`
39. `tests/vault/test_vault003_live_delete.py::test_runtime_factory_wires_curated_plane_with_immutable_publisher`

## Scope (Yua-approved 2026-07-15 — coupled integration)

The multi-point layout is only correct if EVERY identity consumer resolves the anchor and no write
composition leaves the publisher unwired. This slice therefore owns the coupled integration:
the identity-consumer seams (inventory in
`docs/Musubi/13-decisions/data001-phase2-identity-consumer-inventory.md`) and the three write
compositions (API, lifecycle worker, vault runtime). Same invariant, same Issue (#530) — no new
Issue; this doc is the ownership record.

## Remaining work in this same slice (owned by #530 — units A-rest, C, B; NOT deferred)

These are owned by this coupled slice (no follow-up Issue) and must land before it closes; tracked in
the inventory doc. Still TODO after this checkpoint: the remaining resolve-before-validate
reads (`transitions._lookup_point_id`, `synthesis`, api list `_scroll`, `namespace_stats` count,
`writes_curated` PATCH fence, `recent`), full-layout delete (episodic/curated delete + the two
anchor-id spaces), and anchor-aware retrieval (episodic/curated `query`, `_find_dedup_candidate`,
`hybrid` — rank content, resolve via anchor, post-hydration filter, bounded overfetch/underfill).
