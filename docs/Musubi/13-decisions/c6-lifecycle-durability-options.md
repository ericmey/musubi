---
title: "C6: lifecycle audit durability — design-options memo"
section: 13-decisions
type: adr
status: proposed
owner: aoi
discoverer: eric
phase: "Lifecycle-audit 2026-07-13 — C6 event-loss"
tags: [type/adr, status/proposed, lifecycle, audit, durability]
updated: 2026-07-13
supersedes: []
---

# C6 — lifecycle audit durability: design-options memo (no src; for Yua's decision)

**Author:** Aoi · 2026-07-13 · **Status:** options memo accompanying the C6 red contract
(`slice-c6-lifecycle-event-loss`, Issue #433). Names the durability model + the exact observability
surface BEFORE source, and draws the C6 / C6b boundary. No source in this slice.

## The hole (verified against `src/musubi/lifecycle/events.py`)

`record()` only appends to an in-RAM buffer; the buffer is written to sqlite by a periodic/count
flush. So an accepted audit event lives ONLY in RAM until the next flush — a process death (or a
failed-then-cleared flush) after the lifecycle mutation committed loses the audit with no signal. The
retry comment is false; `_flush_loop` suppresses with no telemetry; `close()` drops the buffer.

## Durability model — options

- **Option A — durable-on-accept (recommended).** `record()` persists to sqlite synchronously (per
  event or a tiny micro-batch under the lock); the buffer/batch becomes a throughput optimization, NOT
  the durability boundary. *Pro:* an accepted event is durable immediately — closes the crash hole
  directly. *Con:* a sqlite write per record; local sqlite autocommit is sub-ms, and WAL mode
  amortizes fsync — acceptable for lifecycle event volume (small vs the planes it audits).
- **Option B — batch-flush + retention.** Keep the buffer; fix flush to write-before-clear + retain on
  failure + a bounded retry queue. *Pro:* batched writes. *Con:* events remain RAM-only until a flush,
  so this ALONE does NOT close the crash hole — a crash between record and flush still loses them.
- **Option C — durable staging (WAL/append-log on accept, index on flush).** Crash-safe AND batched.
  *Con:* two structures + a recovery/replay path; more surface than the event volume warrants.

**Recommendation:** Option A. It closes the crash hole with the least machinery; B/C add throughput
that the lifecycle event rate does not need. Yua's call.

## Bounded + backpressure policy (all options — canonical audit forbids silent loss)

Under sustained sqlite unavailability the in-memory footprint MUST be bounded (a cap) and `record()`
MUST apply backpressure once it cannot durably accept — **raise / return an error**. Explicitly:
- **NOT drop-oldest** (or any silent drop): audit is canonical; silent loss is forbidden.
- **NOT block forever**: backpressure must be prompt (raise), never an unbounded wait.
So the caller learns "audit could not be accepted" and decides — which is also why the atomicity gap
below is a caller concern, not the sink's.

## Observability (named before source)

- **Metric:** `musubi_lifecycle_event_write_failures_total` on the shared `default_registry` —
  **bounded, no labels** (a plain counter; no per-namespace/object cardinality). Incremented on every
  write failure. Asserted in the contract via the registry scrape delta, never a private attribute.
- **Log:** one ERROR line per failure with **no event body / namespace / object id** (PII-free) — the
  count and the fact of failure, not the audited content.

## Boundary — what C6 closes vs C6b (atomicity)

- **C6 (this slice) closes:** the LifecycleEventSink loses no *accepted* audit event — retention,
  exactly-once retry, explicit shutdown, durable-on-accept crash survival, bounded backpressure, and
  observable failure.
- **C6 does NOT close — track as C6b (or an H5/H7 dependency):** Qdrant-mutation ↔ SQLite-audit
  **atomicity**. `transitions.py:250-268` commits the Qdrant `set_payload` FIRST, then records
  best-effort. Making the sink durable does NOT make the two stores atomic — a crash between the
  mutation commit and even a durable-on-accept audit still leaves mutation-without-audit. Closing that
  needs a transactional-outbox / two-phase / idempotent-replay pattern spanning both stores, which is a
  larger design than sink durability. C6 must not claim to have closed it.
