---
title: "C6b: lifecycle Qdrant↔SQLite atomicity — design (durable-intent outbox, for Yua's ruling)"
section: 13-decisions
type: adr
status: proposed
owner: aoi
discoverer: eric
phase: "Lifecycle-audit 2026-07-13 — C6b atomicity"
tags: [type/adr, status/proposed, lifecycle, audit, atomicity, outbox]
updated: 2026-07-13
supersedes: []
---

# C6b: lifecycle Qdrant↔SQLite atomicity — design (durable-intent outbox, for Yua's ruling)

**Author:** Aoi · 2026-07-13 · **Status:** PROPOSED — design choices + red inventory for Yua's ruling
BEFORE any source (Issue #437). Slice: [[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]]. C6
durability is accepted + merged-pending ([[13-decisions/c6-lifecycle-durability-options]]); this ADR
settles the cross-store atomicity C6 explicitly did **not** close, and specifies the behavior-shaped red
contract to be written once the architecture is ruled.

## The gap (verified against `src/musubi/lifecycle/transitions.py`)

`transition()` (l.133-286): reads current state/version from Qdrant → checks legality + version →
builds `new_payload` + `event` → **`client.set_payload(...)` mutates Qdrant FIRST (l.252-256)** → **then
`sink.record(event)` (l.267-268)**. Two independent failure/crash windows:

1. **Crash/kill between the Qdrant commit and the audit write** → mutation-without-audit (the mutation
   happened; the audit never landed). C6's durable-on-accept does NOT close this — it only guarantees an
   *accepted* audit is not lost, not that the audit and the mutation are atomic.
2. **`expected_version` is warn-only (l.180-187: "last writer wins")** — a stale writer (or a replay)
   silently clobbers a newer state. An outbox that *replays* mutations makes this actively dangerous.

The docstring's "we retry the sink write on flush; the mutation is idempotent" is the same false-retry
claim C6 already disproved on the sink side; here it also papers over the atomicity gap.

## Core finding — does `record(event)` stay a final-append primitive? **NO.**

Durable-intent-before-mutation is **unachievable with a pure final-append `record()`**: to survive a
crash between the mutation and the audit, the audit *intent* must be durable **before** the Qdrant
mutation, and confirmed **after**. That is inherently a two-call boundary. Recommendation:

- **Add a begin/finalize outbox API** on the sink (names for ruling):
  - `begin_transition(intent) -> Result[PendingHandle, LifecycleOutboxError]` — writes a **PENDING**
    outbox row and COMMITS it, before any Qdrant mutation. Durable intent.
  - `finalize_transition(handle, outcome) -> Result[None, LifecycleOutboxError]` — after the Qdrant
    mutation, marks the row **APPLIED→FINAL** (success) or leaves it **PENDING** (mutation failed,
    retryable) or **ABANDONED** (fence stale / illegal on replay).
- **`record(event)` REMAINS** — as (a) the primitive for audit events with **no external mutation**
  (pure state-machine appends that have no atomicity partner) and (b) the internal "append a FINAL row"
  that `finalize_transition` calls. So C6's contract is untouched; `transitions.py` migrates from
  `record()` to `begin/finalize`.

**Alternative considered + rejected:** keep `record()` final-append and bolt a separate reconcile pass
that infers missing audits from Qdrant. Rejected — it cannot distinguish "mutation applied, audit lost"
from "mutation never happened" without a durable pre-mutation intent, so it can neither replay safely nor
avoid false audits. The intent row is load-bearing.

## State machine (outbox row)

```
        begin_transition                 finalize(applied)
  ∅ ─────────────────▶ PENDING ───────────────────────▶ APPLIED ──▶ FINAL
                          │  ▲                                         (terminal, audit-true)
       finalize(failed)   │  │ reconciliation replay/confirm
       (mutation refused) │  │
                          ▼  │
                    (stays PENDING, retryable)
                          │
       fence stale / illegal-on-replay
                          ▼
                      ABANDONED  (terminal; audited as not-applied, PII-free reason code)
```

- **PENDING** — intent durable; Qdrant outcome unknown/unconfirmed. The only state that is retryable.
- **APPLIED** — Qdrant mutation CONFIRMED committed (by version+payload readback). Audit-true.
- **FINAL** — terminal success; the C6 durable audit row is written exactly here (or APPLIED collapses
  to FINAL when `finalize` runs inline and confirms).
- **ABANDONED** — terminal non-apply; the intent could not/should not be applied. Audited as not-applied.

Guarded transitions only (SQL `UPDATE ... WHERE state = <expected>`), so every edge is idempotent.

## Crash matrix (the invariant: a FINAL audit ⟺ a confirmed Qdrant mutation)

| # | crash point | on restart, reconciliation must… | forbidden |
|---|---|---|---|
| C1 | after PENDING, **before** Qdrant | query Qdrant; if not applied and fence valid → **replay** the mutation, else **ABANDON** | a FINAL row with no matching mutation |
| C2 | after Qdrant, **before** finalize | query Qdrant; version+payload match → **APPLIED→FINAL** | losing the audit (the C6b hole) / leaving it PENDING forever |
| C3 | after FINAL | no-op (terminal) | double-apply / duplicate FINAL |
| C4 | mid-reconciliation | lease reclaim (below); resume | two workers finalizing the same row |

## The 12 must-settle points → decisions

1. **Durable intent before any Qdrant mutation** — YES; `begin_transition` commits PENDING first.
2. **pending/applied/final state machine** — as above (+ ABANDONED terminal).
3. **Full crash matrix** — C1–C4 above; the aggregate invariant is FINAL ⟺ confirmed mutation.
4. **Idempotent replay / event_id** — `event_id` is the idempotency key; Qdrant `set_payload` to the
   same target payload is idempotent; guarded SQL state edges make replay exactly-once (one FINAL row).
5. **Expected-version fencing / concurrency** — **DECISION for ruling:** promote `expected_version` from
   warn-only to a **hard fence for audited transitions** — `begin_transition` refuses (Err + ABANDONED)
   when `current_version != expected_version`, and the mutation is version-conditional, so a stale
   *replay* cannot clobber a newer state. (Changes today's "last writer wins" for the audited path;
   legacy warn-only stays available only for the no-audit `record()` path. Flag for compat review.)
6. **SQLite unavailable ⇒ no Qdrant mutation** — guaranteed by ordering: PENDING commit precedes the
   mutation; if the PENDING write fails, `transition()` returns Err and never calls `set_payload`.
7. **Qdrant failure ⇒ durable retryable intent, no false final audit** — the PENDING row persists
   (retryable); NO APPLIED/FINAL is written; caller gets `Err(retryable=True, durable_intent=True)`;
   reconciliation later replays.
8. **Reconciliation worker leases/recovery** — a worker claims PENDING rows older than a threshold via
   an atomic guarded UPDATE setting `lease_owner`/`lease_expires_at`; only the holder resolves the row;
   an **expired** lease (dead worker) is reclaimable. Bounded attempts → ABANDONED with a PII-free code
   after N tries.
9. **Caller Result semantics** — `transition()`/`begin`/`finalize` return `Result[T, E]` (AGENTS.md
   l.105 — never raise at the boundary); the error carries `retryable` + `durable_intent` so a caller
   learns the audit intent survived a Qdrant failure.
10. **Bounded backpressure / telemetry / PII** — the pending backlog is durable in SQLite (not RAM), so
    "bounded" = a `musubi_lifecycle_outbox_pending` gauge + reconciliation-lag/attempt counters, all
    **no-label / PII-free** (codes only, never namespace/object_id/reason). **Optional backpressure:**
    `begin_transition` may refuse new transitions (Err, bounded) once pending depth exceeds a cap —
    flagged for ruling (default: alert-only, do not refuse).
11. **Migration / rollback** — **DECISION for ruling:** a **separate `lifecycle_outbox` table**
    (`event_id PK, object_id, collection, intended_payload, expected_version, state, attempts,
    lease_owner, lease_expires_at, created_epoch, updated_epoch`) rather than overloading
    `lifecycle_events` — keeps C6's clean FINAL-audit table separate from in-flight outbox state.
    Migration is additive (CREATE TABLE IF NOT EXISTS); rollback must **drain PENDING first** (cannot
    drop the table with live intents) — the rollback path is itself a red.
12. **`record()` final-append vs begin/complete** — **begin/finalize required; `record()` retained** for
    the no-mutation path (see Core finding). Do not assume record() suffices.

## Behavior-shaped RED INVENTORY (to be written on ruling; strict-xfail against current no-outbox code)

Fixtures: in-memory Qdrant (`QdrantClient(":memory:")`, seeded object), real SQLite outbox+sink, fault
injectors for `set_payload` (Qdrant) and the PENDING write (SQLite), a crash subprocess whose crash point
is env-selected, and a reconciliation entrypoint. Every wait/join/subprocess bounded so a regression
fails rather than hangs.

Reds (≈13):
- R1 `test_durable_intent_persisted_before_qdrant_mutation` — PENDING row exists for `event_id` even when
  the Qdrant mutation is faulted (today: no pre-mutation intent).
- R2 `test_sqlite_unavailable_blocks_qdrant_mutation` — fault the PENDING write ⇒ Qdrant version
  UNCHANGED + Err (today: Qdrant mutates before any sink write).
- R3 `test_qdrant_failure_leaves_retryable_pending_not_final` — fault `set_payload` ⇒ PENDING persists,
  NO FINAL audit, `Err(retryable, durable_intent)` (today: Err with the audit never recorded).
- R4 `test_crash_after_intent_before_qdrant_no_false_final` — subprocess crash at C1; reconcile applies+
  finalizes OR abandons — never a FINAL without a matching mutation.
- R5 `test_crash_after_qdrant_before_finalize_recovers_audit` — subprocess crash at C2; reconcile
  detects the applied mutation and finalizes (the C6b hole: today the audit is lost).
- R6 `test_idempotent_replay_single_final_row` — replay a PENDING twice ⇒ exactly one FINAL row + one
  effective apply.
- R7 `test_expected_version_hard_fence_blocks_stale_replay` — a replay whose `expected_version` no longer
  matches is ABANDONED, not applied over the newer state (today: warn-only clobber).
- R8 `test_reconciliation_lease_prevents_double_processing` — two concurrent reconcilers ⇒ a row is
  resolved exactly once.
- R9 `test_reconciliation_reclaims_expired_lease` — a dead worker's expired lease is reclaimed and the
  row still finalizes.
- R10 `test_transition_error_flags_durable_intent_on_qdrant_failure` — caller Result carries
  `retryable`/`durable_intent` on Qdrant failure.
- R11 `test_outbox_pending_metric_bounded_and_pii_free` — pending depth observable via a no-label metric;
  reconciliation decrements it; no namespace/object_id/reason in metric or log.
- R12 `test_no_false_final_audit_invariant_across_crash_matrix` — aggregate: FINAL rows ⟺ confirmed
  mutations across C1–C3.
- R13 `test_outbox_schema_migration_additive_and_rollback_drains_pending` — migration is additive;
  rollback refuses/drains while PENDING rows exist.

Guards (green): callsite inventory (transition() is the only begin/finalize caller) + AST "Result
consumed" for `begin`/`finalize` (extends C6 item 8).

Red-proof plan: a temporary minimal outbox (begin→PENDING commit, version-conditional `set_payload`,
finalize→FINAL, a single-pass reconcile with leases) flips all reds to `XPASS(strict)`; source restored,
zero `src/` committed.

## Open choices flagged for Yua's ruling

1. **API names/shape** — `begin_transition`/`finalize_transition` on the sink, vs a separate
   `LifecycleOutbox` object, vs `record_pending`/`record_final`.
2. **Expected-version → hard fence** for the audited path (changes "last writer wins"). Recommend yes.
3. **Separate `lifecycle_outbox` table** vs columns on `lifecycle_events`. Recommend separate.
4. **Backpressure**: refuse-on-cap vs alert-only. Recommend alert-only default.
5. **Reconciliation driver**: in-process daemon (like C6's flusher, but durable-backed) vs an
   externally-triggered sweep. Recommend externally-triggered + a thin optional daemon.

No source, no host, no merge until these are ruled.
