---
title: "ADR 0007: Every State Change Emits a LifecycleEvent"
section: 13-decisions
tags: [adr, lifecycle, observability, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0007: Every State Change Emits a LifecycleEvent

**Status:** accepted
**Date:** 2026-03-16
**Deciders:** Eric

## Context

Memories, concepts, and curated docs change state over time:

- An episodic memory matures from `fresh` → `warm` → `stale`.
- A concept gets reinforced, then promoted or rejected.
- A curated doc gets demoted or archived.
- An artifact gets re-chunked after edit.

In the POC, state changes happened in-place: `set_payload` on the affected point, no trail. Debugging "why did this memory disappear?" meant guessing: was it deduped? Merged? Deleted? Forgotten by the user? Each case looked identical in Qdrant after the fact.

For a system that auto-mutates data on the user's behalf, that's unacceptable. We need an audit trail.

## Decision

**Every state transition emits a `LifecycleEvent` row to a `lifecycle_events` sqlite table.** No in-place mutations happen without a corresponding event.

Schema (summary; see [[06-ingestion/lifecycle-engine]]):

```
lifecycle_events
  id              text pk (ulid)
  ts              text iso8601
  object_kind     text   # memory | concept | curated | artifact | thought
  object_id       text
  from_state      text   # nullable on create
  to_state        text   # nullable on delete
  actor           text   # job name | user | presence id
  reason          text   # short human-readable
  metadata_json   text   # structured extras (scores, thresholds, etc.)
```

Jobs that change state **do three things atomically-enough** (best-effort in-process):

1. Mutate the point/file.
2. Append a `lifecycle_events` row.
3. Publish an in-process event so subscribers (metrics, reflections) can react.

## Alternatives

**A. Log state changes to structured logs only.** Logs are transient; events are queryable. Both have a place — we do both.

**B. Use Qdrant payload history (timestamps on every field).** Partial — doesn't capture *why*, doesn't capture deletions.

**C. Full event sourcing (Qdrant is derived from an event log).** Too heavy; the point *is* the current state. Events are supplemental.

**D. No formal record; rely on git for vault + snapshots for Qdrant.** Loses the "why" (git has diffs; doesn't have reasons). Loses in-flight state (snapshots are periodic, not continuous).

## Consequences

- Debugging answers "who mutated X and why?" directly from `lifecycle_events`.
- Reflection jobs can summarize activity ("yesterday: 42 maturations, 3 promotions, 0 contradictions").
- Operators get a retention knob: `lifecycle_events` older than 180 days get archived out of the hot sqlite file.
- Tests can assert: "after maturation, exactly one event row with `to_state=warm` exists."

Trade-offs:

- Writes are ~1.5x cost (point mutation + event row). At our scale, fine.
- Another surface to keep healthy (sqlite file, rotation, size monitoring).

## Links

- [[06-ingestion/lifecycle-engine]]
- [[06-ingestion/index]]
- [[10-security/audit]]
- [[09-operations/asset-matrix]]
