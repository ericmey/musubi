---
owner: claude-code-opus48
status: in-progress
issue: 502
title: "Slice: RET-008 concurrency-safe access accounting"
slice_id: slice-ret008-concurrent-accounting
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
# Slice: RET-008 concurrency-safe access accounting

## Context

RET-002's `account_delivered` used a batched read-modify-write; Qdrant has no atomic increment
and reports no matched-count, so under real parallelism (multiple workers/processes, a future
async client, or a concurrent cross-process writer such as the lifecycle service) it loses
increments. Verified on real Qdrant: 8 parallel deliveries → final `access_count` 1, not 8.

Within TODAY's single-process (`--workers 1`) deployment the synchronous Qdrant client blocks the
event loop across a whole read→write, so same-loop deliveries already serialize (guarded); the
exposure is cross-process + future multi-worker/async. Fix: #502.

## Invariant (Yua, 2026-07-15)

Concurrent deliveries must not lose increments; exactly-final-delivered semantics for HTTP,
stream, context; exact namespace+object identity; no N+1; fail-loud on exhaustion.

## Mechanism — fenced per-record lease (single internal field, no other schema)

`store/access_lease.py::lease_increment_access` (shared seam): ACQUIRE a record's lease by writing
`access_lease_token = issued_at_us:nonce` filtered on the token being EMPTY (fresh) or matching the
EXACT observed EXPIRED token (crash-recovery takeover — never a blind steal); confirm we hold it
and it is still fresh; then increment `access_count` + clear the lease in ONE update fenced on
`access_lease_token == ours` — a stale/taken-over holder's fenced write matches zero, so it can
never corrupt the counter. A fresh lease cannot be taken over, so the fenced increment is
guaranteed to apply. Batched per collection (scroll + acquire + readback + increment/release per
round); bounded retry + jitter; exhaustion raises (`AccessLeaseExhausted`) and is finalized as a
typed `Err` by `orchestration.retrieve` (INTERNAL APIError at `/v1/context`).

**Prior mechanisms rejected (verified):** CAS on the counter with a token-readback is RACY — the
single token slot gets CLOBBERED by the next legitimate winner before the prior winner reads back
(over/under-count under 8-way stress). The lease is clobber-safe because losers are fenced out.

## Writer inventory (route-or-prove — a bypassing writer invalidates the mechanism)

Every active production `access_count` writer, verified:
- `retrieve.accounting.account_delivered` — routes through the lease seam.
- `EpisodicPlane.get(bump_access=True)` (direct fetch, `GET /v1/episodic/{id}`) — routes through
  the lease seam (was an inline RMW).
- `EpisodicPlane.query()` — **no production callers** (verified: streaming moved to orchestration
  in RET-002). Dead → cannot race. Must route if reactivated.
- `ConceptPlane.mark_accessed()` — **no production callers** (verified). Dead → cannot race.
- lifecycle `demotion` — **FILTERS** on `access_count==0` (read), does NOT write it. No race.

## Specs to implement
- [[05-retrieval/orchestration]] — § 9 (access accounting; concurrency via the shared lease)

## Owned paths
- `src/musubi/store/access_lease.py`
- `tests/store/test_access_lease.py`
- `tests/retrieve/test_ret008_concurrent_accounting_integration.py`
- `docs/Musubi/_slices/slice-ret008-concurrent-accounting.md`

## Forbidden paths
- Any schema beyond the single `access_lease_token` field; wire/API contract changes; authorization;
  lifecycle-state semantics.

## Modified (owned by shipped slices — coordinated via the lock)
- `src/musubi/types/base.py` (types) — the single `access_lease_token` nullable field (exclude=True).
- `src/musubi/retrieve/accounting.py` (RET-002) — `account_delivered` delegates to the lease seam.
- `src/musubi/planes/episodic/plane.py` (slice-plane-episodic) — `get` bump routes through the seam.
- `tests/retrieve/test_ret002_access_accounting.py` (RET-002) — N+1 test updated for the lease's
  bounded batched writes (acquire + increment, not one).

## Test Contract (real Qdrant unless noted)
- `test_parallel_deliveries_lose_no_increment` — old RMW threaded RED → green.
- `test_eight_way_delivery_final_count_exact` — repeated 8-way exact count.
- `test_nonexpired_lease_cannot_be_stolen` — a live lease is never stolen (fail-loud instead).
- `test_expired_lease_exact_token_takeover_recovers` — expired exact-token takeover / crash recovery.
- `test_old_holder_fenced_after_takeover` — old holder's fenced write matches zero post-takeover.
- `test_update_and_release_atomic_readback` — increment + release land together.
- `test_lease_exhaustion_is_fail_loud` (unit) — bounded exhaustion raises a typed error.
- `test_single_loop_deliveries_stay_correct` (unit) — single-event-loop correctness guard.
- RET-002 HTTP/stream/context suites — semantics unchanged.

## Definition of Done
- No lost/duplicated increment under real 8-way parallelism; all writers routed or proven no-race.
- Single internal field only; wire/OpenAPI additive (field excluded from serialization).
- Full gate green; real-Qdrant proofs green; exact-head CI.

## Work log
- Verified on real Qdrant: RMW loses increments; Qdrant filtered set_payload is atomic CAS; no
  matched-count; model `extra=forbid`; `--workers 1` both deploys; single event loop already
  serializes same-process deliveries.
- Rejected CAS-on-counter (token clobber race, verified over/under-count) → pivoted to the fenced
  lease (clobber-safe; verified robust 8-way ×6).
- Inventoried every access_count writer; routed the two active ones (accounting seam, episodic.get)
  through the shared lease; proved query/mark_accessed dead and demotion read-only.

### Out-of-scope: pre-existing `05-retrieval/orchestration` Test Contract bullets

This slice cites `[[05-retrieval/orchestration]]` for the accounting seam. That spec's structural/
concurrency/timeout/determinism/integration bullets are pre-existing gaps owned by the shipped
`slice-retrieval-orchestration` (follow-up **Issue #509**), not introduced or in scope for RET-008.
Declared out-of-scope so the Closure Rule is honestly machine-green; NOT implemented here:
`test_fast_mode_skips_rerank`, `test_deep_mode_invokes_rerank`, `test_fast_mode_skips_lineage_hydrate`,
`test_deep_mode_hydrates_when_flag_true`, `test_steps_run_in_documented_order`,
`test_planes_run_in_parallel`, `test_hydrate_fetches_run_in_parallel`,
`test_whole_call_timeout_fast_400ms`, `test_per_plane_timeout_deep_1500ms`,
`test_rerank_timeout_returns_with_warning`, `test_deterministic_for_fixed_inputs`,
`test_tiebreak_on_object_id`.
