---
owner: claude-code-opus48
status: in-review
issue: 500
title: "Slice: RET-002 final-delivery access accounting"
slice_id: slice-ret002-access-accounting
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-review
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
- `test_non_accountable_plane_delivery_is_noop` (parameterized artifact/thought — direct, deterministic)
- `test_account_delivered_scopes_to_exact_namespace_object_id_pair` (exact (ns,object_id), collision guard)
- `test_retrieve_normalizes_accounting_failure_to_typed_err` (fail-loud, Result contract)
- `test_context_accounting_failure_returns_internal_not_raw` (fail-loud, INTERNAL APIError)
- `test_accounting_is_batched_per_collection_not_n_plus_1`
- `test_streaming_retrieval_accounts_each_delivered_row_once` (HTTP+streaming share the seam)
- `test_context_accounts_only_surfaced_pack_items_not_dropped_candidates` (/v1/context accounts the trimmed final pack, not retrieval candidates)

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
- Copilot closeout (PR #508): `/v1/context` retrieves `candidate_limit` candidates then trims
  via `build_context_pack` (max_items/max_chars/filler), so accounting-in-`retrieve()` counted
  trimmed candidates. Fixed with an `account_access` (default True) kwarg on
  `orchestration.retrieve`; `/v1/context` passes False and accounts the flattened FINAL pack
  items itself. `/v1/retrieve` + `/v1/retrieve/stream` unchanged (their delivered set is the
  envelope). Because accounting consumes the post-build pack, a candidate dropped for ANY
  reason is unaccounted by construction; the new test discriminates this via the `max_items`
  trim (max_chars/filler pack-trimming is covered by existing context_pack unit tests, not
  re-integration-tested here).
- Copilot #2/#4/#5 folded (one commit): (#2) `account_delivered` now scopes each write to the
  EXACT delivered `(namespace, object_id)` pair via a `should`-of-`must` filter — one read/write
  per collection preserved, plus a collision discriminator. (#4/#5) the vacuous artifact/thought
  no-op tests replaced by a deterministic parameterized test that hands `account_delivered` a stub
  row directly. (#3) best-effort was REJECTED intentionally — accounting drives lifecycle and must
  stay fail-loud — but raw exceptions violated `retrieve()`'s `Result` contract, so an accounting
  failure now normalizes to `Err(RetrievalError kind='internal')` in orchestration and an INTERNAL
  `APIError` in `/v1/context`, with bounded detail; one failure-contract test per seam.

### Out-of-scope: pre-existing `05-retrieval/orchestration` Test Contract bullets

This slice cites `[[05-retrieval/orchestration]]` for its access-accounting bullets (19–28,
all realized here). The spec's OTHER bullets are pre-existing gaps owned by the shipped
`slice-retrieval-orchestration`, NOT introduced or in scope for RET-002 — they were already
missing before this slice touched the spec. Their real follow-up is **Issue #509**. Declared
out-of-scope here (per Yua, 2026-07-15) so the Closure Rule is honestly machine-green; NOT
implemented in this slice:

- `test_fast_mode_skips_rerank` — pre-existing, out-of-scope; follow-up #509.
- `test_deep_mode_invokes_rerank` — pre-existing, out-of-scope; follow-up #509.
- `test_fast_mode_skips_lineage_hydrate` — pre-existing, out-of-scope; follow-up #509.
- `test_deep_mode_hydrates_when_flag_true` — pre-existing, out-of-scope; follow-up #509.
- `test_steps_run_in_documented_order` — pre-existing, out-of-scope; follow-up #509.
- `test_planes_run_in_parallel` — pre-existing, out-of-scope; follow-up #509.
- `test_hydrate_fetches_run_in_parallel` — pre-existing, out-of-scope; follow-up #509.
- `test_whole_call_timeout_fast_400ms` — pre-existing, out-of-scope; follow-up #509.
- `test_per_plane_timeout_deep_1500ms` — pre-existing, out-of-scope; follow-up #509.
- `test_rerank_timeout_returns_with_warning` — pre-existing, out-of-scope; follow-up #509.
- `test_deterministic_for_fixed_inputs` — pre-existing, out-of-scope; follow-up #509.
- `test_tiebreak_on_object_id` — pre-existing, out-of-scope; follow-up #509.
- `integration: end-to-end fast-path on 10K corpus with real TEI + Qdrant, p95 ≤ 400ms` — integration, out-of-scope; follow-up #509.
- `integration: end-to-end deep-path with rerank, NDCG@10 on golden set ≥ threshold` — integration, out-of-scope; follow-up #509.
- `integration: kill TEI mid-request, pipeline returns with documented degradation` — integration, out-of-scope; follow-up #509.
