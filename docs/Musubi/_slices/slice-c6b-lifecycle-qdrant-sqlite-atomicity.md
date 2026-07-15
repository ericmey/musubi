---
title: "Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)"
slice_id: slice-c6b-lifecycle-qdrant-sqlite-atomicity
section: _slices
type: slice
status: done
owner: aoi
phase: "Lifecycle-audit 2026-07-13 — C6b atomicity design + red contract"
tags: [section/slices, status/done, type/slice, lifecycle, audit, atomicity]
updated: 2026-07-14
reviewed: true
depends-on: []
blocks: ["[[_slices/slice-c6-lifecycle-event-loss]]", "[[_slices/slice-h5-unify-state-mutation]]", "[[_slices/slice-c6b-phase1-source-impl]]"]
issue: 437
---

# Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)

**DONE** (2026-07-14). Tracked as Issue #437 (distinct from #433, which is C6 only). Phase 1 source
implementation merged via PR #455 at `dd0f971` (S1–S7: coordinator wiring, Pending semantics, S6 rollback,
maintenance barrier). H5/G1 closure merged via PR #473 at `7e5864d` (canonical coordinator boundary, five
plane writers migrated, eight production callers consume Final/Pending/Err; G1 exact denominator moved from
six to zero). The separate legacy FILE-to-DIR lifecycle storage migration utility is preserved and
deferred in Issue #474 — it is **not** a C6b completion gate (C6b core is landed via PRs #455 and #473;
the FILE-to-DIR utility is a separate legacy-layout maintenance item, deferred by Eric on 2026-07-14
until after the active code-fix ledger is complete).

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
  (dead workers) are reclaimable. A transient/unknown failure is retried with **bounded backoff** and a
  **durable `attempts` count** (observability only) — it is **never** abandoned by attempt count. Only a
  **proven-terminal** classification → `ABANDONED`. `attempts` is not a retry cap: the only pending-depth
  bound is R14's **global hard cap**, which gates *storage admission* (`begin` → `Err(cap_exceeded)`) and
  never terminates a retry. "Within the hard cap" means admission only.

## Relationship to C6

- **Blocks:** the C6 **source** slice (durable-on-accept implementation of `record()`). That source must
  not merge before C6b is reviewable — the AST/callsite contract in
  [[_slices/slice-c6-lifecycle-event-loss]] proves the caller *consumes* the new `Result`, which is the
  seam this slice builds on.
- **Does not block:** the C6 tests-only red contract, which is additive and carries zero `src/musubi`.

Decision context: [[13-decisions/c6-lifecycle-durability-options]] (§ "Boundary — what C6 closes vs C6b").

## Test Contract (behavior-shaped red inventory v2 — being encoded)

Full inventory + fixtures + red-proof plan: [[13-decisions/c6b-lifecycle-atomicity-design]] § "Behavior-shaped
RED INVENTORY v2". 22 strict-xfail reds (R1–R22) + 3 guards, each labeled **Phase-1-acceptance** or
**closure-gate**:
durable-intent-before-mutation (R1), sqlite-blocks-qdrant (R2), transient⇒Ok(Pending) (R3),
terminal⇒Err/ABANDONED-no-FINAL (R4), crash matrix C1/C2/C3 (R5/R6/R7), finalize one-txn atomicity (R8),
idempotent replay (R9), operation_key caller-retry idempotency (R10), single active intent (R11), hard
version fence (R12), conditional apply + full readback (R13), hard cap (R14), transient-never-abandoned
(R15), lease + expired-reclaim (R16/R17), no poison-row starvation (R18), PII-free minimal-patch content
(R19), rollback-refuses-nonterminal (R20), three-way caller outcome (R21), **two-different-transitions
race — loser cannot mutate/overwrite (R22)**. Guards: G2 coordinator callsite inventory + G3 AST
"TransitionOutcome consumed" (Phase-1); **G1 — RED today, closure-gate — AST/rg forbidding direct
`state`-writing `set_payload` outside the coordinator** (enumerates the **6** post-create transition bypass
violators — `transitions.py::transition` + the 5 plane `transition()` methods, per the committed
`_PRESENT_TRANSITION_BYPASSES` control; the maturation/synthesis/demotion `set_payload`s are non-state
enrichment/contradiction/reinforcement writes and are correctly EXCLUDED; flips green
only under [[_slices/slice-h5-unify-state-mutation]]). R1–R22 + G2 + G3 are **Phase-1 source acceptance**
(flip green with the coordinator impl); **G1 is defect closure** (green only under H5). Fixtures:
in-memory Qdrant (`QdrantClient(":memory:")`), real shared SQLite events+outbox, transient/terminal
`set_payload` + PENDING-write fault injectors, env-selected crash subprocess (C1/C2/C3), reconciliation
entrypoint.

## Phase 1 vs defect closure + H5 (correction G; Yua sequencing 2026-07-13)

`transitions.py` is not the only mutation path: the `state`-writing transition bypasses are **6 sites
across 6 files** — `transitions.py::transition` + the 5 plane `transition()` methods (the committed
`_PRESENT_TRANSITION_BYPASSES` G1 inventory: episodic, concept, thoughts, artifact, curated). The
maturation/synthesis/demotion `set_payload`s write **non-state** enrichment/contradiction/reinforcement
fields and are correctly EXCLUDED (repair 3) — they are not state writers. **No circular dependency** —
the relationship is two-phase and acyclic:

- **C6b Phase 1** = the `LifecycleTransitionCoordinator` + `LifecycleOutbox` API + implementation. It
  lands with **C6b still OPEN**. Phase-1 source acceptance is proven by reds R1–R22 + guards G2/G3 (they
  flip green with the coordinator implementation).
- **[[_slices/slice-h5-unify-state-mutation]]** (Issue #439) then **consumes** the coordinator API and
  migrates every bypassing path. H5 `depends-on` C6b (it needs the API); it does **not** `blocks` C6b in
  the DAG (that would be circular). C6b `blocks` H5. **The real gate: H5 is WITHHELD until the C6b source
  phases S1–S7 land AND the caller `Pending` semantics are specified** — the internal maturation /
  non-HTTP caller contract where `Pending` = DEFERRED
  ([[13-decisions/c6b-phase1-source-cut-plan]] §F source-commit series + § "Internal caller contract").
  H5 has no coordinator to migrate onto until the full `coord.transition()` exists (S3) and the seam is
  wired (S7), and it cannot define migration behavior for a `Pending` outcome whose caller semantics are
  still unspecified.
- **Defect closure** = guard **G1** (AST/rg forbidding direct `state`-writing `set_payload` outside the
  coordinator) goes green. G1 is **RED today** and stays RED through Phase 1 — it flips green **only when
  H5 lands**. So C6b closes as a defect only after H5. C6b does NOT claim atomicity for the canonical
  maturation/API paths alone.

The red contract labels each red as **Phase-1-acceptance** (R1–R22, G2, G3) or **closure-gate** (G1).

## Status

**`done`** (2026-07-14) — C6b lifecycle atomicity contract closed. The **22-red + 3-guard** tests-only
red contract (R1–R22 + G1/G2/G3) is **encoded and accepted** (commit `c7b95da`, Issue #437, PR #434 draft on
`slice/c6-lifecycle-event-loss-red`); the **Phase-1 source implementation is merged to `main` in PR #455
at `dd0f971`** (S1–S7: coordinator wiring, Pending semantics, S6 rollback, maintenance barrier, exact-head
CI + Vault check + Publish + Release Please all green); **H5/G1 closure merged in PR #473 at `7e5864d`**
(canonical coordinator boundary, five plane writers migrated, eight production callers consume
Final/Pending/Err; G1 exact denominator moved from six to zero). Blocked-by H5 (#439) lifted via #473.

The separate legacy FILE-to-DIR lifecycle storage migration utility is **preserved and deferred** in
**Issue #474** ("Deferred: legacy lifecycle SQLite FILE-to-DIR migration utility"). It is **not a C6b
completion gate**: C6b core (coordinator/atomicity) is landed via PRs #455 and #473; the FILE-to-DIR
utility is a separate legacy-layout maintenance item, deferred by Eric on 2026-07-14 until after the
active code-fix ledger is complete. Current production is already DIR-only; the legacy FILE path is
absent; the utility is not required to complete C6b or the current code-fix closeout. Resume only after
Eric explicitly reprioritizes.
