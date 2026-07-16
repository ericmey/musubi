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

## Specs to implement

- [[_slices/slice-data001-phase2-immutable-vectors]] — this slice's contract is its `## Test Contract`
  below (self-referential, like slice-c6-lifecycle-event-loss): the store invariant core plus the
  Yua-approved coupled integration (identity consumers + the three write compositions).

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

Identity-consumer + composition discriminators in extended shared files (function in backticks; file
path as evidence):

34. `test_presence_and_raw_payload_ignore_orphan_content_shell` — tests/planes/test_raw_lookup_and_delete.py
35. `test_get_missing_id_does_not_bump_access` — tests/planes/test_episodic.py (resolve-before-bump)
36. `test_concurrent_dedup_race_resolves_to_single_winner` — tests/planes/test_episodic.py (anchor-aware get)
37. `test_same_id_update_inherits_state_lineage_access_from_fresh` — tests/planes/test_curated.py
38. `test_bootstrap_wires_write_planes_with_immutable_publisher` — tests/api/test_bootstrap.py
39. `test_runtime_factory_wires_curated_plane_with_immutable_publisher` — tests/vault/test_vault003_live_delete.py

Unit A-rest identity-consumer discriminators (transitions / synthesis / API list / namespace_stats /
curated PATCH / recent — resolve authoritative identity before validate/count/project):

40. `test_transition_identity_lookup_excludes_content` — tests/store/test_data001_phase2_identity_consumers.py
41. `test_namespace_stats_route_counts_one_identity_per_v2_object` — tests/store/test_data001_phase2_identity_consumers.py
42. `test_scroll_namespace_excludes_content_and_underfills_on_dangling` — tests/store/test_data001_phase2_identity_consumers.py
43. `test_scroll_namespace_cursor_survives_dangling_underfill` — tests/store/test_data001_phase2_identity_consumers.py
44. `test_recent_resolves_v2_and_skips_dangling` — tests/store/test_data001_phase2_identity_consumers.py
45. `test_synthesis_resolve_candidate_no_torn_read` — tests/store/test_data001_phase2_identity_consumers.py
46. `test_non_anchor_plane_is_one_scroll_raw` — tests/retrieve/test_recent.py
47. `test_patch_metadata_preserves_concurrent_state_access_bumps_version_once` — tests/planes/test_curated.py
48. `test_patch_curated_router_refuses_dangling_pointer_without_mutation` — tests/planes/test_curated.py

Unit C full-layout episodic delete (remove the complete v1/v2 layout across BOTH id spaces + all
content; content-before-identity ordering; canonical cross-namespace refusal + corrupted-row
removability + retry truth):

49. `test_delete_removes_v1_layout` — tests/store/test_data001_phase2_identity_consumers.py
50. `test_delete_removes_converted_v2_layout` — tests/store/test_data001_phase2_identity_consumers.py
51. `test_delete_removes_brand_new_v2_layout` — tests/store/test_data001_phase2_identity_consumers.py
52. `test_delete_removes_all_content_generations` — tests/store/test_data001_phase2_identity_consumers.py
53. `test_delete_wrong_namespace_refuses_with_zero_deletion` — tests/store/test_data001_phase2_identity_consumers.py
54. `test_delete_corrupt_identity_payload_still_removable` — tests/store/test_data001_phase2_identity_consumers.py
55. `test_delete_not_found_and_retry_truth` — tests/store/test_data001_phase2_identity_consumers.py
56. `test_delete_content_failure_preserves_identity_then_retry_removes_all` — tests/store/test_data001_phase2_identity_consumers.py

Unit B episodic anchor-aware retrieval (`query` + `_find_dedup_candidate` + the shared ranked-read seam;
rank content + v1 never anchors, resolve via anchor, state POST-hydration, bounded overfetch, fail-closed
on malformed):

57. `test_resolve_ranked_candidate_classification` — tests/store/test_data001_phase2_identity_consumers.py
58. `test_query_forced_anchors_never_consume_the_ranked_budget` — tests/store/test_data001_phase2_identity_consumers.py
59. `test_query_ranks_v1_and_healthy_v2` — tests/store/test_data001_phase2_identity_consumers.py
60. `test_query_rejects_stale_higher_scoring_content` — tests/store/test_data001_phase2_identity_consumers.py
61. `test_query_dangling_and_unknown_kind_fail_closed` — tests/store/test_data001_phase2_identity_consumers.py
62. `test_query_state_filter_is_post_hydration` — tests/store/test_data001_phase2_identity_consumers.py
63. `test_query_malformed_candidate_fails_closed` — tests/store/test_data001_phase2_identity_consumers.py
64. `test_dedup_walks_past_many_stale_to_the_live_candidate` — tests/store/test_data001_phase2_identity_consumers.py

Unit B curated anchor-aware retrieval + vault seams #8/#9/#12 (query state+bitemporal POST-hydration on
the typed row; identity seams fail-LOUD — raise / typed `invalid_row`):

65. `test_query_returns_v1_and_healthy_v2` — tests/store/test_data001_phase2_curated_retrieval.py
66. `test_query_rejects_stale_higher_scoring_content` — tests/store/test_data001_phase2_curated_retrieval.py
67. `test_query_dangling_v2_fails_closed` — tests/store/test_data001_phase2_curated_retrieval.py
68. `test_query_bitemporal_window_is_post_hydration` — tests/store/test_data001_phase2_curated_retrieval.py
69. `test_query_malformed_validity_is_skipped_not_500` — tests/store/test_data001_phase2_curated_retrieval.py
70. `test_private_find_by_vault_path_absent_returns_none` — tests/store/test_data001_phase2_curated_retrieval.py
71. `test_private_find_by_vault_path_resolves_v2_identity` — tests/store/test_data001_phase2_curated_retrieval.py
72. `test_private_find_by_vault_path_dangling_raises` — tests/store/test_data001_phase2_curated_retrieval.py
73. `test_private_find_by_vault_path_content_shell_does_not_shadow` — tests/store/test_data001_phase2_curated_retrieval.py
74. `test_public_find_by_vault_path_v2_single_identity` — tests/store/test_data001_phase2_curated_retrieval.py
75. `test_public_find_by_vault_path_two_identities_multiple_matches` — tests/store/test_data001_phase2_curated_retrieval.py
76. `test_public_find_by_vault_path_dangling_is_invalid_row` — tests/store/test_data001_phase2_curated_retrieval.py
77. `test_public_find_by_vault_path_absent_is_not_found` — tests/store/test_data001_phase2_curated_retrieval.py
78. `test_scan_counts_v2_object_once_excluding_content` — tests/store/test_data001_phase2_curated_retrieval.py
79. `test_scan_dangling_identity_raises` — tests/store/test_data001_phase2_curated_retrieval.py
80. `test_supersession_of_v2_anchor_no_layout_validation_error` — tests/store/test_data001_phase2_curated_retrieval.py

Unit B watcher `invalid_row` fail-closed (only `not_found` is the clean no-op; `invalid_row` + any
unknown code warn + refuse):

81. `test_delete_broken_or_unknown_code_warns_and_refuses` (parametrized invalid_row / unknown) — tests/vault/test_vault003_live_delete.py

Unit B hybrid collection-gated anchor-aware retrieval (must_not anchor both legs + top-level; state +
curated bitemporal POST-hydration on the validated model; plane-safe skip; RRF score preserved; bounded
overfetch; concept/thought/artifact byte-unchanged):

82. `test_hybrid_ranks_v1_and_healthy_v2` — tests/retrieve/test_data001_phase2_hybrid.py
83. `test_hybrid_rejects_stale_higher_scoring_content` — tests/retrieve/test_data001_phase2_hybrid.py
84. `test_hybrid_anchor_never_ranks_on_either_leg` — tests/retrieve/test_data001_phase2_hybrid.py
85. `test_hybrid_dangling_fails_closed` — tests/retrieve/test_data001_phase2_hybrid.py
86. `test_hybrid_state_filter_is_post_hydration` — tests/retrieve/test_data001_phase2_hybrid.py
87. `test_hybrid_malformed_authoritative_is_skipped` — tests/retrieve/test_data001_phase2_hybrid.py
88. `test_hybrid_curated_expired_v2_excluded_live_included` — tests/retrieve/test_data001_phase2_hybrid.py
89. `test_hybrid_curated_ranks_v1_and_healthy_v2` — tests/retrieve/test_data001_phase2_hybrid.py
90. `test_hybrid_non_anchor_plane_takes_raw_path_no_resolver` (parametrized concept/thought/artifact) — tests/retrieve/test_data001_phase2_hybrid.py
91. `test_hybrid_gated_filters_carry_must_not_anchor_non_gated_do_not` — tests/retrieve/test_data001_phase2_hybrid.py
92. `test_hybrid_bounded_underfill_reaches_live_past_higher_stale` — tests/retrieve/test_data001_phase2_hybrid.py

## Scope (Yua-approved 2026-07-15 — coupled integration)

The multi-point layout is only correct if EVERY identity consumer resolves the anchor and no write
composition leaves the publisher unwired. This slice therefore owns the coupled integration:
the identity-consumer seams (inventory in
`docs/Musubi/13-decisions/data001-phase2-identity-consumer-inventory.md`) and the three write
compositions (API, lifecycle worker, vault runtime). Same invariant, same Issue (#530) — no new
Issue; this doc is the ownership record.

## Remaining work in this same slice (owned by #530 — NONE; all units landed)

Owned by this coupled slice (no follow-up Issue). **All units DONE + proven:**

- **Invariant core** (tests 1–33) + path-audit corrections + `embed_kind` projection.
- **Unit D** — the three write compositions inject the publisher (API bootstrap, lifecycle runner, vault
  runtime); episodic/curated `get()` anchor-aware.
- **Unit A-rest** — resolve-before-validate identity reads (transitions / synthesis / `_scroll` /
  `namespace_stats` / `writes_curated` PATCH / `recent`).
- **Unit C** — full-layout episodic delete (both anchor-id spaces + all content, content-before-identity).
- **Unit B** — anchor-aware retrieval: episodic `query` + `_find_dedup_candidate`, curated `query` +
  vault seams `_find_by_vault_path` / `find_by_vault_path` / `scan_vault_rows`, and gated `hybrid_search`
  (rank content, resolve via anchor, state + curated-bitemporal POST-hydration on the validated model,
  bounded overfetch, fail-closed skip; identity seams fail-LOUD; concept/thought/artifact byte-unchanged),
  the vault-watcher `invalid_row` branch, and the discovered curated-supersession-over-v2-anchor fix (D1).

Inventory: all 19 original seams + D1 reconciled to **DONE + proven** in
`docs/Musubi/13-decisions/data001-phase2-identity-consumer-inventory.md`. Test Contract entries 1–92
above. This slice is ready for handoff to review (no self-merge).
