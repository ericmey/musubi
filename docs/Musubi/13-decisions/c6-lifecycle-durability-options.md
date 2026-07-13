---
title: "C6: lifecycle audit durability — accepted decision (Option A, durable-on-accept)"
section: 13-decisions
type: adr
status: accepted
owner: aoi
discoverer: eric
decided-by: yua
phase: "Lifecycle-audit 2026-07-13 — C6 event-loss"
tags: [type/adr, status/accepted, lifecycle, audit, durability]
updated: 2026-07-13
supersedes: []
---

# C6: lifecycle audit durability — accepted decision (Option A, durable-on-accept)

**Author:** Aoi · **Decided by:** Yua (2026-07-13) · **Status:** ACCEPTED — durable-on-accept.
This ADR fixes the durability model and the exact observability surface BEFORE source, and draws the
C6 / C6b boundary. No source in this slice; the contract is `slice-c6-lifecycle-event-loss` (Issue #433).

## The hole (verified against `src/musubi/lifecycle/events.py`)

`record()` only appends to an in-RAM buffer; the buffer is written to sqlite by a periodic/count flush.
So an accepted audit event lives ONLY in RAM until the next flush — a process death (or a
failed-then-cleared flush) after the lifecycle mutation committed loses the audit with no signal. The
retry comment (L.158-160) is false; `flush()` clears before it writes (L.114-118); `_flush_loop`
suppresses with no telemetry (L.161); `close()` drops the buffer via the `_closed` early-return
(L.197-198).

## Decision — Option A, durable-on-accept

`record()` persists the event to sqlite **synchronously** and returns `Result[None,
LifecycleEventWriteError]` — an event is "accepted" ONLY after COMMIT (per AGENTS.md l.105: a mutation
at a module boundary returns a Result, never a raised/suppressed exception). The RAM buffer and the
background flusher are **removed as a durability mechanism**.

- **Accepted (committed) ⇒ durable immediately.** No window where an accepted event lives only in RAM,
  so the crash hole is closed directly.
- **A failed write is refused, not queued.** `record()` returns `Err` immediately; nothing is retained
  in memory. Because failures are refused at the boundary, **there is NO retry queue and NO backpressure
  cap** — nothing accumulates, so nothing needs bounding. (The earlier "bounded buffer + block /
  drop-oldest backpressure policy" framing is withdrawn: it only existed to bound a RAM queue this model
  does not have. Silent loss was never on the table; refuse-immediately is strictly simpler than any cap.)
- **Cost:** one sqlite write per record. Local sqlite autocommit is sub-ms and lifecycle event volume is
  small relative to the memory planes it audits, so per-event commit is acceptable. A micro-batch under
  the lock remains available later as a pure throughput optimization — NOT the durability boundary.

**Rejected alternatives (for the record):** *batch-flush + retention* (events stay RAM-only until a
flush, so a crash between record and flush still loses them — does not close the hole); *durable staging
/ append-log-then-index* (crash-safe and batched, but two structures + a recovery/replay path, more
machinery than this event rate warrants).

## Observability (named before source)

- **Metric:** `musubi_lifecycle_event_write_failures_total` on the shared `default_registry` —
  **bounded, no labels** (a plain counter; no per-namespace/object cardinality), rendered as exactly one
  unlabeled series. Incremented by exactly +1 on every write failure. Asserted in the contract via the
  rendered exposition delta, never a private attribute.
- **Log:** one ERROR line per failure with **no reason/body, no namespace, and no object_id** (PII-free)
  — the count and the fact of failure, not the audited content.

## Boundary — what C6 closes vs C6b (atomicity)

- **C6 (this slice) closes:** the LifecycleEventSink loses no *accepted* audit event — success is
  immediately durable; failure is a refused `Err` with zero rows + the observable metric/log; retry of
  the same `event_id` is idempotent (exactly one row); crash and close cannot lose an Ok event.
- **C6 does NOT close — tracked as C6b (an H5/H7 dependency, named + linked before any C6 source
  merge):** Qdrant-mutation ↔ SQLite-audit **atomicity**. `transitions.py:250-268` commits the Qdrant
  `set_payload` FIRST, then records. Durable-on-accept does NOT make the two stores atomic — a crash
  between the mutation commit and even a durable-on-accept audit still leaves mutation-without-audit.
  Closing that needs a transactional-outbox / two-phase / idempotent-replay pattern spanning both stores,
  a larger design than sink durability. C6 must not claim to have closed it. Contract + inventory:
  [[_slices/slice-c6-lifecycle-event-loss]].
