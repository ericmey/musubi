---
title: "Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)"
slice_id: slice-c6b-lifecycle-qdrant-sqlite-atomicity
section: _slices
type: slice
status: blocked
owner: aoi
phase: "Lifecycle-audit 2026-07-13 — C6b atomicity dependency"
tags: [section/slices, status/blocked, type/slice, lifecycle, audit, atomicity]
updated: 2026-07-13
reviewed: false
depends-on: []
blocks: []
issue: 433
---

# Slice: C6b lifecycle audit — Qdrant↔SQLite atomicity (precondition of C6 source merge)

The concrete follow-on that C6's durability work explicitly does **not** close. Named + linked here so
the dependency is reviewable **before any C6 source merge** — durable-on-accept must not be mistaken for
cross-store atomicity. Status `blocked`: no contract yet, and it cannot proceed until C6 source is
scheduled. It exists so the boundary is a tracked artifact, not prose.

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

**`blocked`** (2026-07-13) — boundary artifact only; no contract, no source; cannot proceed until C6
source is scheduled. Owner: aoi. A design memo + red contract follow then. Tracking Issue #433 (shared
lifecycle-audit epic; a dedicated C6b issue to be bootstrapped when this leaves `blocked`).
