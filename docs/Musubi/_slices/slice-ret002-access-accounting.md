---
owner: aoi
status: in-progress
issue: 500
title: "Slice: RET-002 final-delivery access accounting"
slice_id: slice-ret002-access-accounting
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: RET-002 final-delivery access accounting

## Context

Access accounting (which stored rows were *used*) was a side effect of deep lineage
hydration: `EpisodicPlane.get(bump_access=True)` reached only from `deep._hydrate_one`,
only when `include_lineage=True`, and only for episodic. So fast/recent never accounted,
concept/curated never accounted, deep-without-lineage accounted nothing, and blended
accounted candidates later dropped by dedup/limit. Lifecycle decisions were fed incorrect
usage data. Parent audit: #411. Fix: #500.

## Invariant (Yua, 2026-07-15)

After fanout / dedup / sorting / limit, account each **final delivered** row exactly once;
never account a dropped candidate; identical whether `include_lineage` is true or false.
Accountable planes are those whose type (`MemoryObject`) carries `access_count` — episodic,
curated, concept. artifact and thought (`MusubiObject`) intentionally lack the field and are
an explicit, tested no-op (no schema expansion). Concurrency stays batched read-modify-write;
true concurrent-counter safety is tracked separately as **Issue #502** and is NOT solved here.

## Specs to implement
- [[05-retrieval/orchestration]] — § 9 "Account access (final delivery boundary)"

## Owned paths
- `src/musubi/retrieve/accounting.py`
- `tests/retrieve/test_ret002_access_accounting.py`
- `tests/api/test_ret002_streaming_access.py`
- `docs/Musubi/_slices/slice-ret002-access-accounting.md`

## Forbidden paths
- Plane storage/lifecycle semantics beyond the read-path bump decoupling; no schema/index
  changes (no `access_count` added to artifact/thought); no wire/API changes; no atomic
  counter work (that is #502). No touching C4 / STREAM / DQ / Fresh-memory.

## Modified (owned by shipped slices — coordinated via the lock)
- `src/musubi/retrieve/orchestration.py` (slice-retrieval-orchestration, done) — one async
  accounting step in `retrieve()` immediately after `_finalize`.
- `src/musubi/retrieve/deep.py` (slice-retrieval-deep, done) — `bump_access=False` at both
  hydration and lineage-walk episodic `get()` sites (decouple accounting from hydration).
- `docs/Musubi/05-retrieval/orchestration.md` (slice-retrieval-orchestration, done) — new
  step 9 + Test Contract bullets.
- `tests/retrieve/test_ret007_envelope.py` (RET-007) — `_MockQdrant` gains no-op
  `scroll`/`batch_update_points` to satisfy the new final-boundary client contract.

## Test Contract
Discriminating red matrix (each fails current main, passes the correction), plus two no-op
guards (green before and after) and a streaming proof:
- `test_delivered_episodic_row_accounted_once_per_mode` (fast / deep / blended / recent)
- `test_deep_include_lineage_false_still_accounts_delivered`
- `test_deep_accounting_identical_regardless_of_include_lineage`
- `test_limit_drop_accounts_only_delivered_not_dropped_candidates`
- `test_delivered_curated_row_accounted`
- `test_delivered_concept_row_accounted`
- `test_delivered_artifact_row_is_explicit_noop`
- `test_delivered_thought_row_is_explicit_noop`
- `test_accounting_is_batched_per_collection_not_n_plus_1`
- `test_streaming_retrieval_accounts_each_delivered_row_once` (HTTP+streaming share the seam)

## Definition of Done
- Accounting runs once at the final delivery boundary; hydration no longer accounts.
- episodic/curated/concept delivered rows accounted exactly once; artifact/thought no-op.
- include_lineage true/false accounting identical; dropped candidates untouched.
- One batched read + one batched write per accountable collection (no N+1).
- Typed results/warnings unchanged. `make check` full pass; exact-head CI green.

## Work log
- Grounded the exact current call graph (single trigger: `episodic.get(bump_access)` via
  `deep._hydrate_one`) and verified the plane-type access_count matrix by introspection.
- Wrote the discriminating red matrix FIRST; proved 9 RED + 4 correctly-green on base
  `5b53693` before any src change; implemented the decouple + shared batch seam; all green.
- Concurrent-counter safety deliberately out of scope → Issue #502.
