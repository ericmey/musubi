# DATA-001 Phase 2 — identity-consumer inventory (#530)

Work-log companion to [`data001-phase2-immutable-vectors.md`](./data001-phase2-immutable-vectors.md).
The multi-point layout (v2 ANCHOR + write-once CONTENT points, or a v1 legacy row) changes what
*every* consumer of a `(namespace, object_id)` in the `musubi_episodic` / `musubi_curated`
collections sees. This table is the grep-backed sweep of those seams and the behavioral fix each
needs. Concept / thought / artifact planes have no anchors and are out of scope.

## Key discriminator (why a seam breaks or not)

A v2 **CONTENT** point carries only `object_id, namespace, point_kind="content", generation,
owner_token, content, summary, [title]` — **no** `state, version, *_epoch, importance, access_count,
reinforcement_count, vault_path, supersedes/superseded_by`. A v2 **ANCHOR** carries the full mutable
payload **plus** `point_kind="anchor", live_point, pointer_version, committed_operation_id,
vector_layout_version` and a **zero vector** (brand-new) or a **stale legacy vector** (converted
in place). A **v1** legacy row = full payload, no `point_kind`, real vector. Fresh rows are v1 until
a vector-changing reinforce/update converts them.

Consequences:

- A filter on a discriminating field (`state`, `*_epoch`, `importance`, `vault_path`, …) **auto-excludes**
  content shells — safe *iff* the consumer reads raw `.get()`; **breaks if it `model_validate`s** (the
  anchor carries extra Phase-2 keys and the models are `extra="forbid"`).
- A filter on **only `namespace`/`object_id`** matches BOTH anchor and content → double-count /
  arbitrary-point / `model_validate` blow-up.
- **State-filtered VECTOR queries silently drop the real vectors** — the meaningful dense/sparse
  vectors live on content points, which lack `state`; the state `must` excludes them and leaves the
  zero/stale-vector anchor. This is the central retrieval-correctness break, not just a validate nuisance.

## Two cross-cutting rules for the fixes

1. **Two anchor-id spaces.** A converted-in-place object keeps its legacy id
   (`uuid5(_POINT_NS, object_id)`); a brand-new object's anchor is at `anchor_point_id =
   uuid5(_ID_NS, …)`. Any id-addressed read/delete is wrong for at least one origin — **prefer
   payload-filtered anchor resolution over id derivation.**
2. **Resolve, then validate.** Never `model_validate` a scrolled/queried payload directly from these
   collections — resolve the authoritative identity (anchor-over-content, or v1) first.

## MUST FIX

| # | seam (file:line, func) | op | risk | fix | status |
|---|---|---|---|---|---|
| 1 | `lifecycle/coordinator.py:872 _read_object` (→ `_persist_event:902`, `_apply_conditional/_confirm:1005`, `_cur:1371`) | scroll ns+oid limit=2 | anchor+content → count=2 → every episodic/curated transition fences/abandons | exclude `point_kind=content` (no-op for v1 + concept/thought/artifact) | **DONE + proven** (test 24, red-proofed) |
| 2 | `store/raw_lookup.py:71 raw_payload` | scroll ns+oid limit=1 | returns arbitrary point (may be content shell) | target identity (`_identity_by_id`, must_not content); v2 anchor carries content so this IS anchor-over-content | **DONE + proven** (orphan-shell test) |
| 2b | `store/raw_lookup.py:52 point_exists` (**promoted from Already-safe** — Yua/Tama) | scroll ns+oid limit=1 | an orphan content shell (anchor missing/deleted) reports the object PRESENT → existence guards keep a half-deleted object alive | exclude content (identity presence only) | **DONE + proven** (orphan-content discriminator) |
| 3 | `store/immutable_vectors.py delete_object_layout` (was `raw_lookup.retrieve_by_point_id`) | delete full layout | brand-new anchor at a different id → None (delete 404); converted anchor single-delete orphans content | centralized `delete_object_layout` + `read_identity_payload` address BOTH id spaces + fan out every content point | **DONE + proven** (unit C: `test_delete_removes_brand_new_v2_layout`, `test_delete_removes_all_content_generations` — identity_consumers) |
| 4 | `planes/episodic/plane.py get()` | resolve then bump | validated an anchor/content shell → raises; cascades to patch/transition/reinstate | `resolve_committed_content` before validate; access bump only after resolve | **DONE + proven** (healthy v2: `test_episodic_get_resolves_healthy_v2`; dangling → None + zero bump: `test_episodic_get_dangling_v2_returns_none_without_access_bump` — identity_consumers; absence-no-bump: `test_get_missing_id_does_not_bump_access` — test_episodic) |
| 5 | `planes/episodic/plane.py _find_dedup_candidate` | query dense, ns filter | ranks content + stale converted anchors; returns shell → validate fail / stale candidate | shared seam: must_not anchor, `ranked_dedup_budget`, per-candidate `resolve_ranked_candidate`, safe-validate | **DONE + proven** (`test_dedup_walks_past_many_stale_to_the_live_candidate` — identity_consumers) |
| 6 | `planes/episodic/plane.py query()` | query dense, ns+state | content excluded by state; anchors (zero/stale vec) surface → validate fail + real vectors unreachable | anchor-aware: must_not anchor prefilter, bounded overfetch, resolve, state POST-hydration | **DONE + proven** (`test_query_ranks_v1_and_healthy_v2`, `test_query_forced_anchors_never_consume_the_ranked_budget`, `test_query_state_filter_is_post_hydration` — identity_consumers) |
| 7 | `planes/episodic/plane.py delete()` | delete full layout | misses relocated anchor; orphans content | `delete_object_layout` — anchor/v1 in BOTH id spaces + every content point, content-first ordering | **DONE + proven** (unit C: `test_delete_removes_v1_layout` / `_converted_v2_layout` / `_brand_new_v2_layout` — identity_consumers) |
| 8 | `planes/curated/plane.py _find_by_vault_path` | scroll ns+vault_path, must_not content | anchor carries vault_path → returns anchor → validate fail; cascades to create dedup | must_not content, resolve via anchor; distinguish absent(None) vs dangling/malformed(RAISE) | **DONE + proven** (`test_private_find_by_vault_path_absent_returns_none` / `_resolves_v2_identity` / `_dangling_raises` / `_content_shell_does_not_shadow` — curated_retrieval) |
| 9 | `planes/curated/plane.py find_by_vault_path` | scroll vault_path limit=2, must_not content | anchor payload → validate fail; content shells inflate count | count DISTINCT identities; dangling/malformed → typed `invalid_row` (never clean not_found) | **DONE + proven** (`test_public_find_by_vault_path_v2_single_identity` / `_two_identities_multiple_matches` / `_dangling_is_invalid_row` / `_absent_is_not_found` — curated_retrieval) |
| 10 | `planes/curated/plane.py get()` | resolve then validate | shell → validate fail; cascades to transition | `resolve_committed_content` + strip before validate | **DONE + proven** (healthy v2: `test_curated_get_resolves_healthy_v2`; dangling → None: `test_curated_get_dangling_v2_returns_none` — curated_retrieval) |
| 11 | `planes/curated/plane.py query()` | query dense, ns + must_not anchor | content excluded by state/bitemporal prefilter; anchors surface | anchor-aware: must_not anchor, resolve, state + bitemporal POST-hydration on the TYPED validated row | **DONE + proven** (`test_query_returns_v1_and_healthy_v2` / `_rejects_stale_higher_scoring_content` / `_dangling_v2_fails_closed` / `_bitemporal_window_is_post_hydration` / `_malformed_validity_is_skipped_not_500` — curated_retrieval) |
| 12 | `planes/curated/plane.py scan_vault_rows` | scroll must_not content, resolve each | iterated content shells + anchors → validate fail | must_not content (double-count + budget defense), resolve via anchor, FAIL-LOUD (raise) on dangling/malformed | **DONE + proven** (`test_scan_counts_v2_object_once_excluding_content` / `_dangling_identity_raises` — curated_retrieval; `test_scan_vault_rows_surfaces_validation_failure` — test_curated) |
| 13 | `retrieve/hybrid.py` (gated: `_build_prefetch` / `_build_filter` / `_hits_from_response`) | prefetch+fusion | content excluded by state; anchors surface on either leg; hits carry extra keys | collection-gated: must_not anchor on BOTH legs + top-level, state (+curated bitemporal) POST-hydration on validated model, plane-safe skip, RRF score preserved, bounded overfetch; concept/thought/artifact byte-unchanged | **DONE + proven.** Behavioral (LOAD-BEARING): `test_hybrid_anchor_never_ranks_on_either_leg` (forced anchor tops both legs → still excluded), `test_hybrid_rejects_stale_higher_scoring_content` (stale snapshot outscores → live wins), `test_hybrid_bounded_underfill_reaches_live_past_higher_stale`, `test_hybrid_ranks_v1_and_healthy_v2`, `_state_filter_is_post_hydration`, `_malformed_authoritative_is_skipped`, `_curated_expired_v2_excluded_live_included`, `_curated_ranks_v1_and_healthy_v2`. Structural: `_gated_filters_carry_must_not_anchor_non_gated_do_not`, `_non_anchor_plane_takes_raw_path_no_resolver` (— test_data001_phase2_hybrid). |
| 14 | `lifecycle/synthesis.py _resolve_candidate_memory` | single-read anchor-aware | anchors → validate fail; retrieve-by-legacy-id misses brand-new anchors | resolve per candidate (single snapshot, no torn read); address by object_id | **DONE + proven** (`test_synthesis_resolve_candidate_no_torn_read` — identity_consumers) |
| 15 | `api/routers/_scroll.py scroll_namespace` | scroll must_not content, resolve+strip | anchors+shells → validate 500s; page counts doubled | identity page (must_not content), resolve+strip before validate, truthful underfill+cursor | **DONE + proven** (`test_scroll_namespace_excludes_content_and_underfills_on_dangling` / `_cursor_survives_dangling_underfill` — identity_consumers) |
| 16 | `api/routers/namespaces.py namespace_stats count` | count must_not content | count inflated (anchor + N content per object) | count identity rows only (must_not content) | **DONE + proven** (`test_namespace_stats_route_counts_one_identity_per_v2_object` — identity_consumers) |
| 17 | `api/routers/writes_curated.py PATCH` → `plane.patch_metadata` (`owned_update`) | attributable fenced set_payload | unfenced, no ns/version/kind → writes onto content shell + anchor | route via `patch_metadata` (owned_update lease, same-id allowlist); 409 on dangling pointer | **DONE + proven** (`test_patch_metadata_preserves_concurrent_state_access_bumps_version_once` / `test_patch_curated_router_refuses_dangling_pointer_without_mutation` — test_curated) |
| 18 | `lifecycle/transitions.py _scroll_by_object_id / _lookup_point_id` | scroll must_not content | arbitrary shell; admin lineage set_payload may hit content | resolve identity via anchor; return anchor/v1 id only (must_not content) | **DONE + proven** (`test_transition_identity_lookup_excludes_content` — identity_consumers) |
| 19 | `retrieve/recent.py` resolve per identity row | scroll + per-row resolve | returned anchor payloads (extra keys + zero/stale vector) | resolve identity via anchor before handing out; non-anchor plane stays one-scroll-raw | **DONE + proven** (`test_recent_resolves_v2_and_skips_dangling` — identity_consumers; `test_non_anchor_plane_is_one_scroll_raw` — test_recent) |

Also: `api/routers/writes_episodic.py delete` delegates to episodic `plane.delete()` (#7) — inherits its fix. **DONE** (covered by #7).

## Discovered during unit B (not in the original grep sweep — surfaced by the hybrid bitemporal path)

| # | seam (file, func) | op | risk | fix | status |
|---|---|---|---|---|---|
| D1 | `planes/curated/plane.py create()` true-supersession `supersede_plan` | `model_validate({**current, …})` | the OLD row may be a v2 ANCHOR; its fresh payload carries layout-only keys → `extra="forbid"` raises → superseding any body-updated (v2) curated row fails | strip layout fields before validate; the narrow lease write (state + superseded_by + updated_at) is unchanged | **DONE + proven** (`test_supersession_of_v2_anchor_no_layout_validation_error` — curated_retrieval; RED-proved) |

The inventory-completeness discipline (surface a latent gap via a real test collision, not in production) is
the reason this seam was caught before merge. All 19 original seams + D1 are now **DONE + proven**.

## Already safe

- `store/mutation_lease.py` + `store/access_lease.py` — `_EXCLUDE_CONTENT` (`must_not point_kind=content`) on every read/CAS; leases hit only the identity row.
- `store/immutable_vectors.py` — the Phase-2 layer itself (anchor-filtered, fail-closed resolve, full content-fanout delete).
- `api/routers/retrieve.py:348 _expand_wildcard_targets` — reads only `namespace`, dedups into a set.
- `lifecycle/reflection.py`, `maturation.py`, `demotion.py` sweeps — discriminating-field filters exclude content and read raw dicts. **Caveat:** their *apply* rides `coordinator.transition` → depend on seam #1 (now fixed).

## N/A (concept / thought / artifact — no anchors)

`planes/concept/*`, `planes/thoughts/*`, `planes/artifact/*`, `lifecycle/promotion.py`,
`lifecycle/demotion.py` concept/artifact branches, `api/routers/contradictions.py`,
`concepts.py`, `artifacts.py`, and the concept/artifact/thought iterations of `namespace_stats`.

## Pre-existing debt surfaced (RESOLVED)

`tests/lifecycle/test_c6b_atomicity.py::test_r21_route_controls_final_200_and_err_typed[lifecycle|episodic]`
failed at `d07552a` (before seam #1) because `episodic.create → _reinforce` FAILS CLOSED without a wired
publisher. **RESOLVED by unit D:** the `ImmutableVectorPublisher` is now injected into the episodic +
curated planes across all three write compositions (API bootstrap, lifecycle runner, vault runtime) and
the test fixtures (c6b route_env, api/conftest). Proven by `test_bootstrap_wires_write_planes_with_
immutable_publisher` (test_bootstrap) + `test_runtime_factory_wires_curated_plane_with_immutable_publisher`
(test_vault003_live_delete).
