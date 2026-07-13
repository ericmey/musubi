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
depends-on: ["[[_slices/slice-h5-unify-state-mutation]]"]
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

**Design + exact red inventory (v2, ruling applied):** [[13-decisions/c6b-lifecycle-atomicity-design]] —
a durable-intent outbox behind a distinct **`LifecycleTransitionCoordinator`** boundary (+ a distinct
`LifecycleOutbox`, shared SQLite events+outbox DB); `record()` stays a standalone FINAL-append for the
no-mutation path. Three-way caller outcome `Ok(Final)` / `Ok(Pending)` / `Err` (transient Qdrant failure
is Pending, never a false terminal Err); `operation_key` idempotency across caller retries; one active
intent per `(collection,object_id)`; hard canonical version fence + server-side conditional apply with
full readback; expanded crash matrix C1–C6; hard cap; one mandatory reconciliation job. Revised per Yua's
fork rulings + corrections A–J (2026-07-13).

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

## Test Contract (behavior-shaped red inventory v2 — being encoded)

Full inventory + fixtures + red-proof plan: [[13-decisions/c6b-lifecycle-atomicity-design]] § "Behavior-shaped
RED INVENTORY v2". 21 strict-xfail reds (R1–R21) against the current no-outbox path + 3 guards:
durable-intent-before-mutation (R1), sqlite-blocks-qdrant (R2), transient⇒Ok(Pending) (R3),
terminal⇒Err/ABANDONED-no-FINAL (R4), crash matrix C1/C2/C3 (R5/R6/R7), finalize one-txn atomicity (R8),
idempotent replay (R9), operation_key caller-retry idempotency (R10), single active intent (R11), hard
version fence (R12), conditional apply + full readback (R13), hard cap (R14), transient-never-abandoned
(R15), lease + expired-reclaim (R16/R17), no poison-row starvation (R18), PII-free minimal-patch content
(R19), rollback-refuses-nonterminal (R20), three-way caller outcome (R21). Guards: **G1 — RED today —
AST/rg forbidding direct `state`-writing `set_payload` outside the coordinator** (enumerates the ≥8
bypass violators; flips green only under [[_slices/slice-h5-unify-state-mutation]]); G2 coordinator
callsite inventory; G3 AST "TransitionOutcome consumed". Fixtures: in-memory Qdrant
(`QdrantClient(":memory:")`), real shared SQLite events+outbox, transient/terminal `set_payload` +
PENDING-write fault injectors, env-selected crash subprocess (C1/C2/C3), reconciliation entrypoint.

## Structural dependency — H5 (correction G)

`transitions.py` is not the only mutation path: 5 plane `transition()` methods + direct `set_payload`
of `state` in maturation/synthesis/demotion bypass any coordinator. C6b **depends on**
[[_slices/slice-h5-unify-state-mutation]] (Issue #439) to route ALL state mutation through the
coordinator; **C6b atomicity closure is blocked on H5**, and guard G1 stays RED until H5 lands. C6b does
NOT claim atomicity for the canonical maturation/API paths alone.

## Status

**`in-progress`** (2026-07-13) — claimed by aoi (Issue #437, lock in `_inbox/locks/`). Direction accepted
+ design revised to v2 for Yua's fork rulings + corrections A–J. Now encoding the 21-red + 3-guard
behavior contract (zero src). Blocked-by H5 (#439) for closure; blocks C6 (#433 stays C6 only).
