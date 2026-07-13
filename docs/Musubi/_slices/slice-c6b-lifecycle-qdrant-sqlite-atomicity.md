---
title: "Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)"
slice_id: slice-c6b-lifecycle-qdrant-sqlite-atomicity
section: _slices
type: slice
status: in-progress
owner: aoi
phase: "Lifecycle-audit 2026-07-13 — C6b atomicity design + red contract"
tags: [section/slices, status/in-progress, type/slice, lifecycle, audit, atomicity]
updated: 2026-07-13
reviewed: false
depends-on: []
blocks: ["[[_slices/slice-c6-lifecycle-event-loss]]"]
issue: 437
---

# Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)

The concrete follow-on that C6's durability work explicitly does **not** close. Tracked as Issue #437
(distinct from #433, which is C6 only). Status `in-progress` (claimed by aoi, lock
`_inbox/locks/slice-c6b-lifecycle-qdrant-sqlite-atomicity.lock`). It **blocks** the C6 source slice
([[_slices/slice-c6-lifecycle-event-loss]] lists it in `depends-on`), which must not be
authorized/merged before C6b has a design + red contract, because the transition `Err` semantics after a
committed Qdrant mutation are otherwise undefined.

**Design + exact red inventory:** [[13-decisions/c6b-lifecycle-atomicity-design]] — a durable-intent
outbox (PENDING→APPLIED→FINAL / ABANDONED), begin/finalize API (record() stays for the no-mutation
path), full crash matrix, hard version fence, lease-based reconciliation. Returned to Yua for an
architecture ruling BEFORE the red contract is encoded (the C6 rhythm: memo → ruling → contract), because
three forks — begin/finalize vs final-append, hard-fence vs warn-only, separate outbox table vs columns —
change what every red asserts.

## The gap (verified against `src/musubi/lifecycle/transitions.py`)

`transition()` commits the Qdrant mutation FIRST, then records the audit event:

- L.252-256: `client.set_payload(...)` commits the state change to Qdrant.
- L.267-268: `if sink is not None: sink.record(event)` — the audit follows the committed mutation.

Making the sink durable-on-accept ([[_slices/slice-c6-lifecycle-event-loss]]) guarantees that an
*accepted* audit event is never lost, but it does **not** make the two stores atomic. A process death (or
a refused audit write) *between* the Qdrant commit and the audit commit still leaves
**mutation-without-audit** — the exact integrity hole C6 must not claim to have closed.

Symmetrically, once C6's `record()` returns `Result[None, LifecycleEventWriteError]`, the caller can
finally *learn* that an audit was refused after a committed mutation — but deciding what to do about it
(compensate, replay, fail the transition) is this slice's design, not C6's.

## What closing this requires (design — see the ADR for the full treatment)

A durable-intent transactional outbox spanning Qdrant + SQLite — [[13-decisions/c6b-lifecycle-atomicity-design]]:

- **Durable intent BEFORE the mutation:** `begin_transition` writes + commits a PENDING outbox row
  before `set_payload`; `finalize_transition` marks APPLIED→FINAL after. So `record()` cannot stay a
  pure final-append primitive — a two-call boundary is required (record() is retained for no-mutation
  audits). The invariant: **a FINAL audit ⟺ a confirmed Qdrant mutation.**
- **Crash matrix (C1–C4):** intent-before-mutate + version+payload readback on recovery lets
  reconciliation replay (crash before mutate), finalize (crash after mutate, before finalize), or abandon
  — never a false FINAL, never a lost audit.
- **Hard version fence + idempotent replay:** `expected_version` becomes a hard fence for the audited
  path (today warn-only), `event_id` is the idempotency key, guarded SQL edges make replay exactly-once.
- **Lease-based reconciliation:** PENDING rows are claimed via an atomic guarded UPDATE; expired leases
  (dead workers) are reclaimable; bounded attempts → ABANDONED.

## Relationship to C6

- **Blocks:** the C6 **source** slice (durable-on-accept implementation of `record()`). That source must
  not merge before C6b is reviewable — the AST/callsite contract in
  [[_slices/slice-c6-lifecycle-event-loss]] proves the caller *consumes* the new `Result`, which is the
  seam this slice builds on.
- **Does not block:** the C6 tests-only red contract, which is additive and carries zero `src/musubi`.

Decision context: [[13-decisions/c6-lifecycle-durability-options]] (§ "Boundary — what C6 closes vs C6b").

## Test Contract (behavior-shaped red inventory — to be encoded on ruling)

Full inventory + fixtures + red-proof plan: [[13-decisions/c6b-lifecycle-atomicity-design]] § "Behavior-shaped
RED INVENTORY". ≈13 strict-xfail reds against the current no-outbox `transition()` — durable-intent-before-
mutation (R1), sqlite-blocks-qdrant (R2), qdrant-failure-retryable-not-final (R3), crash matrix C1/C2
(R4/R5), idempotent replay (R6), hard version fence (R7), reconciliation lease + expired-lease reclaim
(R8/R9), caller Result durable-intent flag (R10), bounded PII-free pending metric (R11), the FINAL⟺mutation
invariant (R12), migration/rollback (R13) — plus green callsite + AST "Result consumed" guards. Fixtures:
in-memory Qdrant (`QdrantClient(":memory:")`), real SQLite outbox+sink, `set_payload`/PENDING-write fault
injectors, an env-selected crash subprocess, a reconciliation entrypoint. Not yet written: the three design
forks change what each red asserts, so the contract follows Yua's ruling.

## Status

**`in-progress`** (2026-07-13) — claimed by aoi (Issue #437, lock in `_inbox/locks/`). Design + exact red
inventory delivered ([[13-decisions/c6b-lifecycle-atomicity-design]]); returned to Yua for an architecture
ruling before the red contract is encoded and before any source. #433 stays C6 only.
