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
- **Callers assume fire-and-forget.** `transitions.py:250-268`: the Qdrant mutation `set_payload`
  **commits first**, then `if sink is not None: sink.record(event)` runs with **no try/except**, then
  the transition returns `Ok`. Audit is a best-effort side-effect AFTER a committed mutation — so a
  silent background-flush failure leaves the mutation done and the audit erased with nobody informed.
  Consumers (`reflection.py` via `read_all()`) then operate on an incomplete audit trail.

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

## Test Contract

`tests/lifecycle/test_c6_event_loss.py` (deterministic — the background daemon is disabled via
`flush_every_s=3600`; every flush is explicit; a `_FailNThenReal` fault injector controls write
failure):

Control (green now + post-fix):
1. `test_concurrent_record_and_flush_no_loss_no_dup` — lock-protected append/drain is race-free.

Reds (strict-xfail; flip to PASS with the source fix, red-proofed to flip):
2. `test_failed_flush_preserves_events_for_retry` — a failed flush must not discard the batch.
3. `test_successful_retry_writes_each_event_exactly_once` — retry yields each event exactly once.
4. `test_close_does_not_silently_discard_buffered_events` — shutdown persists the buffer.
5. `test_sustained_failure_retains_bounded_not_lost` — sustained failure retains (bounded, named), not lost.
6. `test_swallowed_flush_failure_is_observable` — a suppressed flush failure leaves an observable signal.

**Closure at this head:** 1 passed + 5 xfailed; ruff/mypy/check.py clean; zero `src/musubi`.
Red-proofed: simulating the write-before-clear / retain-on-failure fix flips red #2 to 3/3 green.

## Status

**`in-progress`** (2026-07-13) — red contract only (tests + this doc); the source fix is a SEPARATE
slice after Yua reviews this contract. Tracking Issue #433. Second reader: Tama or Shiori, requested
only after their current lanes clear.

spec-update: slice-c6-lifecycle-event-loss — NEW red contract for C6 lifecycle audit-event loss;
proves failed-flush retention, exactly-once retry, concurrency safety (control), explicit shutdown,
bounded-retention/backpressure (named), and observable failure; source fix deferred (Yua 2026-07-13).
