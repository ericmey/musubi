---
title: "Slice: ART-001 global-search publication discriminator"
slice_id: slice-art-001-global-search-spikes
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

# Slice: ART-001 global-search publication discriminator

Tests/docs-only successor to frozen PRs #452 and #453. It asks one bounded
question: which Qdrant v1.17.1 publication seam can keep a namespace-wide
vector query free of stale/uncommitted rows without losing any current row at
exact K? It does not choose an architecture, authorize source, close Issue
#451, or relax the upload-orchestration prerequisite.

## Owned paths

- `docs/Musubi/_slices/slice-art-001-global-search-spikes.md`
- `docs/Musubi/_inbox/locks/slice-art-001-global-search-spikes.lock`
- `spike-notes/art-001-global-search.md`
- `tests/planes/artifact/test_artifact_global_search_spikes.py`

Everything else is forbidden, especially `src/**`, PR #452/#453 paths, deploy
or host configuration, and the central defect ledger.

## Specs to implement

- [[_slices/slice-art-001-global-search-spikes]] — the numbered test contract
  below is the complete tests/docs-only discriminator contract.

## Acceptance rule

A candidate passes only if every query result has all four properties:

1. exactly K results whenever K current rows exist;
2. zero stale, staged, uncommitted, or losing-owner rows;
3. zero current false negatives at exact K;
4. bounded termination without claiming a snapshot the exercised API did not
   provide.

Concurrent-reader acceptance requires every observed result to be one complete
committed snapshot. A mixture, empty gap, error, short result, or uncommitted
row is a failure.

## Test Contract

Plain controls (unmarked):

1. `test_real_server_and_cross_arch_pins_are_exact` — pinned 1.17.1 server and both architecture digests.
2. `test_adversarial_matrix_names_every_required_visibility_state` — old committed, new staged, winning current, stale-high-score, and losing-owner states are explicit.
3. `test_parent_head_iterative_refill_is_exact_and_bounded_when_quiescent` — static post-validation/refill returns exact K within the finite candidate bound.
4. `test_per_chunk_published_filter_is_exact_when_activation_is_quiescent` — payload filtering works after a fully quiescent activation.
5. `test_complete_collection_alias_cutover_preserves_exact_k_for_concurrent_reader` — an independent reader observes only complete old or complete new exact-K results.
6. `test_process_death_before_during_and_after_activation_reconciles_by_alias_readback` — client death before request, after accepted request with ambiguous response, and after readback reconciles deterministically.
7. `test_activation_retry_readback_and_cleanup_are_deterministic` — retry, alias readback, and old-collection cleanup preserve the winner.
8. `test_complete_alias_candidate_meets_exact_k_safety_and_recall` — a full-query-domain candidate excludes staged/losing rows and retains all K current rows.
9. `test_client_death_is_not_mislabeled_as_a_qdrant_snapshot_or_server_crash` — the harness refuses to call client death an injected server/consensus crash.

Named strict reds (each rejects one wrong candidate; `--runxfail` reaches all six):

10. `test_red_naive_bounded_overfetch_loses_current_exact_k` — fixed 2K overfetch under enough stale high scorers.
11. `test_red_iterative_refill_cannot_claim_concurrent_snapshot` — offset refill while ranking changes between pages.
12. `test_red_flag_activation_crash_after_deactivate_loses_current_exact_k` — deactivate-old-first crash gap.
13. `test_red_flag_activation_exposes_new_before_old_is_fenced` — activate-new-first overlap exposes an uncommitted generation.
14. `test_red_per_artifact_alias_promotion_loses_unaffected_current_rows` — incomplete per-artifact copy behind a global alias.
15. `test_red_ambiguous_client_death_proves_mid_server_crash_atomicity` — fabricated internal-server crash claim.

## Non-closure prerequisites

- Upload still does not invoke indexing. A source slice must own invocation,
  background-job idempotency, failure visibility, and reconciliation.
- A collection alias is collection-scoped. Per-artifact publication in one
  shared query domain would require a complete namespace collection copy and
  serialized blue/green cutover, or a many-collection/fanout design with a
  different scaling and query contract.
- The spike does not inject a Qdrant node/cluster crash during alias consensus.
  It proves independent-reader observations and client-death readback only.
- Random chunk IDs and object-id blob paths remain unreconciled with current
  immutability/content-address claims.
- Generation-less legacy rows still require canonical-blob rebuild; metadata
  counts cannot establish ownership.

## Status

`in-progress`, held and do-not-merge. Decision pending discriminator. A passing
collection-level mechanism is not an ART-001 architecture recommendation or
source authorization.

## Work log

- 2026-07-14: created the isolated discriminator at exact origin/main
  `79cd13eee864983bf5caa93773285a452ced975c`; PRs #452 and #453 remained
  untouched. Pre-encoding real-Qdrant evidence and collection-scope design
  drift were routed to Yua before the first edit.

