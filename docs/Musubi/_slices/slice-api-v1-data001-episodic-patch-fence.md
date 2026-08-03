---
title: "Slice: DATA-001 fenced episodic PATCH"
slice_id: slice-api-v1-data001-episodic-patch-fence
issue: 634
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

# Slice: DATA-001 fenced episodic PATCH

Make episodic PATCH use a namespace-bound, layout-aware, version-fenced,
non-re-embedding mutation. Preserve legacy retraction while v2 embedding-projection
replacement (`content` or `summary`) remains typed-refused pending the Option A
escrow contract in Issue #611.

## Decision boundary

- The live legacy path is the reachable defect: its Qdrant selector binds only
  `object_id`, so duplicate ids can cross namespaces and concurrent writers are
  last-write-wins without attribution.
- V2 does not currently reach that selector. Its raw anchor fails the strict domain
  preflight on storage-only layout keys, so every v2 PATCH is generically refused
  before mutation. Non-projection metadata must become writable; `content` and
  `summary` must receive a typed refusal pointing at #611 because both are stored on
  the immutable embedding snapshot and changing either only on the anchor would red
  VAL-002 with `projection_divergence`.
- Keep content replacement non-embedding. Publishing through the immutable-vector
  content seam would replace the retrieval vector and make a retraction harder to
  find by the false claim it neutralized.
- Branch the fence by layout. A versionless legacy row semantically has version 0
  and must reuse `_legacy_conversion_filter`; v2 must target the exact anchor kind.
- Tags use an explicit `replace` or `merge` mode. The API replaces; the plane helper
  merges. Summary remains replacement semantics.
- Attribution uses the transient `update_lease_token`; release means deleting that
  key, never persisting a null value. The domain model already accepts and excludes
  this internal field so crash recovery can read it; that makes exact key-absence
  assertions, rather than VAL-002, the release invariant.
- The one-shot seam and `owned_update` intentionally coexist. HTTP PATCH must return
  409 to a stale-observation loser so the caller chooses whether to retry;
  `owned_update` is for internal mutations with no caller to make that choice and
  therefore rebases/retries until it lands or exhausts its bounded budget. Choose by
  concurrency contract, never by whichever helper is found first.
- Track the number of episodic anchors in VAL-002 JSON evidence. Zero means the v2
  branch still lacks live estate coverage; the first bootstrap becomes visible.

## Owned paths

- `.github/workflows/integration.yml`
- `Makefile`
- `src/musubi/api/routers/writes_episodic.py`
- `src/musubi/cli/validate.py`
- `src/musubi/planes/episodic/plane.py`
- `src/musubi/store/immutable_vectors.py`
- `docs/Musubi/07-interfaces/canonical-api.md`
- `tests/api/test_data001_episodic_patch_fence.py`
- `tests/cli/test_validate.py`
- `docs/Musubi/_slices/slice-api-v1-data001-episodic-patch-fence.md`
- `docs/Musubi/_inbox/locks/api-v1-data001-episodic-patch-fence.lock`

## Modified under an active owner

- `src/musubi/store/mutation_lease.py`
- `tests/store/test_mutation_lease.py`

Both paths remain owned by `slice-data001-concurrent-full-object-update` while
that slice is in review. This slice changes only the token-release invariant
shared by the old retrying seam and the new one-shot seam: release removes the
transient key and confirms absence rather than persisting a null value.

## Test Contract

1. `test_concurrent_same_version_patch_exactly_one_writer_succeeds_and_loser_gets_typed_conflict`
2. `test_foreign_operation_token_at_same_next_version_cannot_falsely_confirm`
3. `test_same_object_id_in_two_namespaces_changes_only_authorized_namespace`
4. `test_v2_metadata_patch_changes_only_anchor_and_projection_patch_is_typed_refusal`
5. `test_versionless_legacy_row_accepts_exactly_one_expected_version_zero_patch`
6. `test_legacy_content_patch_preserves_vectors_and_is_version_fenced`
7. `test_replace_and_merge_tag_modes_and_summary_replacement_remain_distinct`
8. `test_val002_remains_clean_after_accepted_legacy_and_v2_mutations`
9. `test_authorization_failure_reaches_neither_raw_read_nor_write`
10. `test_validator_reports_episodic_anchor_count`
11. Existing mutation-lease release tests require the transient token key to be
    absent, not merely null-valued.
12. `test_unresolvable_v2_anchor_fails_closed_before_mutation`
13. `test_plane_merge_mode_updates_only_v2_anchor`

## Work log

- 2026-08-03 — Claimed Issue #634 and opened Draft PR #636 from current main.
  The first draft PR auto-closed when the branch was corrected to the repository's
  required `slice/api-v1-*` ownership namespace; it contained no code or tests.
- 2026-08-03 — Aoi and Yua independently refuted the issue's original v2 mutation
  premise before code. A real v2 anchor is rejected by `assert_readable_after_patch`
  on storage-only keys before `set_payload`; a legacy control validates. The issue
  now records the smaller truth: reachable legacy isolation/concurrency defects and
  generic v2 over-refusal. Required target and proof remain unchanged.
- 2026-08-03 — Aoi found that the first test contract treated an absent mutation
  token and a persisted null as equivalent. The contract now requires release by
  key removal. An executable control then refuted the proposed VAL-002 consequence:
  `MemoryObject` already accepts and excludes the internal token, so an orphan does
  not red that sweep. The narrower absence invariant remains the honest final shape.
- 2026-08-03 — The first implementation pass caught a missing projection boundary:
  v2 `summary` is embedded state, not ordinary metadata. The typed #611 refusal now
  covers both `content` and `summary`; tags and importance remain writable.
- 2026-08-03 — Aoi's curated-path comparison exposed a second pre-write gate:
  a v2 anchor must resolve its committed content before any metadata CAS. The
  regression proves a dangling pointer returns 409 with byte-identical rows.
- 2026-08-03 — Implementation gate: focused contract 48 passed; full `make check`
  2505 passed, 195 skipped, 136 deselected, 3 xfailed; the 85% coverage gate
  passed at 88%. The
  integration-only mutation-lease suite could not start because its configured
  Qdrant at localhost:6339 was absent; the in-memory Qdrant API contract exercised
  the new filtered CAS and `delete_payload` release path.
- 2026-08-03 — Filed #637 for curated HTTP PATCH's separate retry-policy mismatch.
  It does not widen this slice: curated already has safe namespace/layout targeting;
  the follow-up only changes HTTP losers from silent rebase to one-shot 409.
- 2026-08-03 — Aoi found that both local `make check` and PR CI deselected the
  real-Qdrant mutation-lease tests, and the integration workflow's manual dispatch
  still targeted only `tests/integration/`. Extended the existing real-service gate
  to include `tests/store/test_mutation_lease.py` and trigger when that module changes;
  a green real-service run over that module must precede merge.
- 2026-08-03 — Kept `make test-integration` equivalent in target set and marker
  selection, with the same default Qdrant port, so the documented local
  gate cannot remain narrower than CI while presenting the same name.
