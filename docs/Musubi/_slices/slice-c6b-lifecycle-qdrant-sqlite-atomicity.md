---
title: "Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)"
slice_id: slice-c6b-lifecycle-qdrant-sqlite-atomicity
section: _slices
type: slice
status: ready
owner: aoi
phase: "Lifecycle-audit 2026-07-13 — C6b atomicity dependency"
tags: [section/slices, status/ready, type/slice, lifecycle, audit, atomicity]
updated: 2026-07-13
reviewed: false
depends-on: []
blocks: ["[[_slices/slice-c6-lifecycle-event-loss]]"]
issue: 437
---

# Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)

The concrete follow-on that C6's durability work explicitly does **not** close. Tracked as Issue #437
(distinct from #433, which is C6 only). Status `ready`: unclaimed, no upstream dependency — any agent may
pick up the design. It **blocks** the C6 source slice ([[_slices/slice-c6-lifecycle-event-loss]] lists it
in `depends-on`), which must not be authorized/merged before C6b has a design + red contract, because the
transition `Err` semantics after a committed Qdrant mutation are otherwise undefined.

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

## What closing this requires (design space — not yet decided)

A transactional-outbox / two-phase / idempotent-replay pattern spanning Qdrant + SQLite:

- **Outbox:** write the audit intent to SQLite in the same logical unit as (or before) the Qdrant
  mutation, then reconcile/replay to Qdrant asynchronously with idempotency on `event_id`.
- **Order + compensate:** audit-first-then-mutate with a durable pending marker, or mutate-then-audit
  with a replay sweep that detects mutation-without-audit and repairs it.
- **Idempotent replay:** `event_id TEXT PRIMARY KEY` + `INSERT OR REPLACE` already make audit replay
  exactly-once; the missing half is a Qdrant-side idempotent reconcile.

Scope + tradeoffs are a full design memo (an H5/H7-class dependency), larger than sink durability.

## Relationship to C6

- **Blocks:** the C6 **source** slice (durable-on-accept implementation of `record()`). That source must
  not merge before C6b is reviewable — the AST/callsite contract in
  [[_slices/slice-c6-lifecycle-event-loss]] proves the caller *consumes* the new `Result`, which is the
  seam this slice builds on.
- **Does not block:** the C6 tests-only red contract, which is additive and carries zero `src/musubi`.

Decision context: [[13-decisions/c6-lifecycle-durability-options]] (§ "Boundary — what C6 closes vs C6b").

## Status

**`ready`** (2026-07-13) — boundary artifact + tracked dependency; no contract or source yet, but
unclaimed and pickup-ready (it blocks C6 source, so it must land first). Owner: aoi. A design memo + red
contract are the next work. Tracking **Issue #437** (dedicated; #433 stays C6 only).
