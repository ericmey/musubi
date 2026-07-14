---
title: "Slice: ART-001 Qdrant OCC discriminator"
slice_id: slice-art-001-qdrant-occ-spikes
issue: 451
section: _slices
type: slice
status: in-progress
owner: tama
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice, spike, qdrant]
updated: 2026-07-14
reviewed: false
depends-on: []
blocks: []
---

# Slice: ART-001 Qdrant OCC discriminator

Tests/docs-only successor to frozen PR #452. It asks one bounded question: can a
Qdrant v1.17.1 operation-record collection plus a conditional head update provide
the ownership, fencing, and restart evidence ART-001 would require? It does not
choose an architecture, authorize source, or close Issue #451.

## Owned paths

- `docs/Musubi/_slices/slice-art-001-qdrant-occ-spikes.md`
- `docs/Musubi/_inbox/locks/slice-art-001-qdrant-occ-spikes.lock`
- `spike-notes/art-001-qdrant-occ.md`
- `tests/planes/artifact/test_artifact_qdrant_occ_spikes.py`

Everything else is forbidden, especially `src/**`, PR #452 paths, deploy/host
configuration, and the central defect ledger.

## Specs to implement

- [[_slices/slice-art-001-qdrant-occ-spikes]] — the numbered test contract below
  is the complete tests/docs-only discriminator contract.

## Test Contract

Plain controls (unmarked):

1. `test_real_server_and_cross_arch_pins_are_exact` — pinned 1.17.1 server and both architecture digests.
2. `test_two_process_conditional_publish_has_one_readback_winner` — independent processes, completed operation results, exact-head winner, owner-scoped loser cleanup.
3. `test_update_only_equality_missing_point_and_stale_retry` — equality/no-match/missing-point/stale retry behavior.
4. `test_fresh_owner_token_blocks_version_reuse_aba` — owner token fences a deliberately reused numeric version.
5. `test_process_death_reconciles_deterministically` — parametrized before-stage, after-stage, before-response, after-ambiguous-response, and before-cleanup deaths.
6. `test_head_then_generation_read_is_per_artifact_consistent` — candidate per-artifact read seam.
7. `test_legacy_mixed_generation_requires_canonical_blob_rebuild` — count inference rejected.

Named strict reds (each rejects one wrong candidate; `--runxfail` reaches all seven):

8. `test_red_completed_status_cannot_identify_the_race_winner` — operation status as winner signal.
9. `test_red_version_only_cannot_distinguish_reused_version` — version-only ABA defense.
10. `test_red_artifact_wide_cleanup_cannot_preserve_winner` — unfenced artifact-wide rollback.
11. `test_red_default_upsert_inserts_when_the_conditional_point_is_missing` — default UPSERT as CAS.
12. `test_red_head_pointer_does_not_solve_namespace_wide_visibility` — head as global vector-query snapshot.
13. `test_red_metadata_count_cannot_partition_mixed_legacy_rows` — metadata count as ownership evidence.
14. `test_red_fork_after_parent_client_is_rejected` — inherited/forked client harness as independent-process evidence.

## Non-closure prerequisites

- Upload currently does not invoke indexing. Any source slice must own invocation,
  background-job idempotency, retry/reconciliation, and visible failure reporting.
- Random chunk IDs and `<namespace>/<object_id>` blob paths conflict with current
  immutability/content-address language. Models, docs, blob identity, migration,
  and backfill must be reconciled together.
- Legacy mixed, generation-less chunks cannot be partitioned safely from metadata
  counts. The safe migration is a rebuild from the canonical blob.

## Status

`in-progress`, held and do-not-merge. Decision pending discriminator. Passing
test-only mechanics are not an architecture recommendation or source authorization.
