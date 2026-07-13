---
title: "Slice: C6 lifecycle audit-event loss — red contract (tests-only)"
slice_id: slice-c6-lifecycle-event-loss
section: _slices
type: slice
status: in-progress
owner: aoi
phase: "Lifecycle-audit 2026-07-13 — C6 event-loss red contract (Yua-authorized, tests-first, zero src)"
tags: [section/slices, status/in-progress, type/slice, lifecycle, audit, durability]
updated: 2026-07-13
reviewed: false
depends-on: []
blocks: []
issue: 433
---

# Slice: C6 lifecycle audit-event loss — red contract (tests-only)

Tests-first red contract for the C6 audit-event-loss defect (Issue #433). **Zero `src/musubi`** — the
source fix follows in a separate slice after Yua reviews this contract. Eric's finding was
independently reproduced against `main` (`src/musubi/lifecycle/events.py`); the prose was confirmed
AND two additional loss paths were found.

## Source observation (what the code DOES today — not the desired behavior)

`src/musubi/lifecycle/events.py`, verified at the merged head:

1. **`flush()` clears before it writes.** L.114-115 (under `_lock`): `pending = self._buffer;
   self._buffer = []`, then L.118 (outside the lock): `self._write_batch(pending)`. If the write
   raises, `pending` is a discarded local and the buffer is already empty → the batch is **lost with
   no path to retry**.
2. **The retry comment is false.** L.158-160 claims "The buffer is preserved for the next interval;
   the next call to `record` or `flush` will retry the write." The code does the opposite.
3. **The failure reaches `flush()`.** `_write_batch` re-raises after `ROLLBACK` (L.211), so the write
   failure genuinely propagates into `flush()` — which has already cleared.
4. **Silent background loss.** `_flush_loop` runs `with contextlib.suppress(Exception): self.flush()`
   (L.161) with **zero** telemetry/log/counter — a background write failure loses events silently.
5. **Shutdown discards the buffer.** `close()` final-drains via `flush()` (L.147), but `_write_batch`
   early-returns on `self._closed` (L.197-198), so the buffered events are dropped on shutdown without
   being written.

## Flush-path inventory + the record()-caller fire-and-forget finding

- **Inline/direct path:** `record()` (L.101-109) appends under lock; when the buffer reaches
  `flush_every_n` (default **100**) it calls `flush()` synchronously in the caller's thread. A failure
  here raises out of `record()`.
- **Background path:** the daemon `_flush_loop` drains every `flush_every_s` (default **5.0s**),
  exception-suppressed. This is the **common** loss case (most records only append; the buffer rarely
  hits 100 within a request).
- **Shutdown path:** `close()` → final `flush()`, which discards via the `_closed` early-return.
- **Callers assume fire-and-forget** (this is the C6b atomicity boundary, NOT a sink loss path C6
  closes). `transitions.py:250-268`: the Qdrant mutation `set_payload` **commits first**, then
  `if sink is not None: sink.record(event)` runs with **no try/except**, then the transition returns
  `Ok`. Audit is best-effort AFTER a committed mutation. Making the SINK durable (this slice) does NOT
  make the two stores atomic — see "What C6 does NOT close" below. Consumers (`reflection.py` via
  `read_all()`) operate on whatever the sink persisted.

## Desired contract (each red is strict-xfail against today)

1. a failed flush preserves every event for retry;
2. a successful retry writes each event exactly once (`event_id TEXT PRIMARY KEY` + `INSERT OR
   REPLACE` make this achievable once events are retained);
3. concurrent `record`/`flush` loses or duplicates nothing (**control** — the lock design must stay
   sound across the fix);
4. shutdown is explicit — `close()` must not silently discard buffered events;
5. under sustained write failure the retained buffer is **bounded** with a **named** backpressure
   policy (block / drop-oldest+counter) — the retention fix must not invent unbounded growth silently;
6. a swallowed flush failure must be **observable** (telemetry/counter/state), never silent success.

## Specs to implement

- [[_slices/slice-c6-lifecycle-event-loss]] — this slice's contract is its `## Test Contract` below.
  At this head the reds are strict-xfail (each reason names the observed defect) and the control
  passes, so `make tc-coverage SLICE=slice-c6-lifecycle-event-loss` exits 0.

## What C6 does NOT close (C6b — atomicity)

Making the sink durable does NOT make Qdrant + SQLite atomic. `transitions.py:250-268` commits the
Qdrant mutation FIRST, then records; a crash between the mutation commit and even a durable-on-accept
audit still leaves **mutation-without-audit**. Closing that needs a transactional-outbox / two-phase /
idempotent-replay pattern spanning both stores — tracked as **C6b (or an H5/H7 dependency)**, not this
slice. Design-options + the durability recommendation: `docs/Musubi/13-decisions/c6-lifecycle-durability-options.md`.

## Test Contract

`tests/lifecycle/test_c6_event_loss.py` — deterministic (background daemon disabled via
`flush_every_s=3600`; a `_FailNThenReal` fault injector; all joins/waits bounded by `_JOIN_TIMEOUT` so
a regression fails instead of hanging CI). Metric named before source:
`musubi_lifecycle_event_write_failures_total` (bounded, no labels), asserted via the shared
`default_registry` scrape delta — never a private attribute.

Control (green now + post-fix):
1. `test_concurrent_record_and_flush_no_loss_no_dup` — lock-protected append/drain is race-free under
   a SUCCESSFUL write (insufficient alone; the failing-write race is red #5).

Reds (strict-xfail; each red-proofed to flip green under the minimal fix):
2. `test_failed_flush_preserves_events_for_retry` — a failed flush must not discard the batch.
3. `test_successful_retry_writes_each_event_exactly_once` — retry yields each event exactly once.
4. `test_close_does_not_silently_discard_buffered_events` — shutdown persists the buffer.
5. `test_failing_write_race_preserves_a_and_b` — batch A failing while B is appended preserves A+B
   (order/cardinality, no loss/dup) — deterministic Event barrier, bounded joins.
6. `test_accepted_events_survive_abrupt_crash` — a subprocess `os._exit` without close()/flush() leaves
   accepted events durable (durable-on-accept).
7. `test_sustained_failure_bounded_backpressure_no_silent_loss` — sustained failure applies backpressure
   (record refuses, not silent accept-then-lose) and loses no accepted event.
8. `test_swallowed_flush_failure_observable_metric_and_log` — a suppressed failure increments the shared
   `musubi_lifecycle_event_write_failures_total` and logs a PII-free ERROR (asserted via registry delta
   + caplog, not a private attribute).

**Closure at this head:** 1 passed + 7 xfailed; ruff/mypy/check.py clean; zero `src/musubi`.
Red-proofed: a temporary minimal fix (write-before-clear + retain + durable-on-accept + backpressure +
counter/log + close-drain) flips ALL 7 reds green (7 XPASS) while the control stays green; source
restored, nothing committed to `src/`.

## Status

**`in-progress`** (2026-07-13) — red contract only (tests + this doc); the source fix is a SEPARATE
slice after Yua reviews this contract. Tracking Issue #433. Second reader: Tama or Shiori, requested
only after their current lanes clear.

spec-update: slice-c6-lifecycle-event-loss — NEW red contract for C6 lifecycle audit-event loss;
proves failed-flush retention, exactly-once retry, concurrency safety (control), explicit shutdown,
bounded-retention/backpressure (named), and observable failure; source fix deferred (Yua 2026-07-13).
