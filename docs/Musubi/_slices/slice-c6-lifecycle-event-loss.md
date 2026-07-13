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
depends-on: ["[[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]]"]
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

## Desired contract — ACCEPTED Option A / durable-on-accept (each red is strict-xfail against today)

Architecture decided by Yua (2026-07-13): `record()` COMMITS the event to sqlite synchronously and
returns `Result[None, LifecycleEventWriteError]`; "accepted" means COMMITTED. The RAM buffer + background
flusher are removed as a durability mechanism; a failed write is refused immediately, so there is **no
retry queue and no backpressure cap** (nothing accumulates in memory). Full decision:
[[13-decisions/c6-lifecycle-durability-options]].

1. a healthy `record()` returns `Ok` and the event is **immediately durable** (readable with no
   flush/close);
2. an injected write failure returns `Err`, persists **zero rows**, increments the shared metric by
   exactly +1 (one unlabeled series), and logs a PII-free ERROR (no reason/namespace/object_id);
3. a retry of the **same `event_id`** after a transient failure returns `Ok` and yields **exactly one
   row** (`event_id TEXT PRIMARY KEY` + `INSERT OR REPLACE`);
4. only `Ok`-accepted events survive an **abrupt crash** — a real subprocess asserts `returncode==0`
   then `os._exit`, and only the committed markers survive on reopen (durable-on-accept);
5. deterministic concurrent successes + one injected failure prove successful records persist **exactly
   once**, the failed record returns `Err`, and there is **no cross-loss**;
6. **sustained 1000 failures** all return `Err` **promptly** — proven in a bounded subprocess so a
   blocking `record()` fails rather than hangs — persisting zero rows with **no in-memory
   queue/backpressure growth** (`_buffer` stays empty);
7. `close()` is **idempotent** and cannot discard an already-`Ok` event;
8. the returned `Result` **must be consumed** at every callsite — a strict AST red rejects the bare
   `sink.record(event)` expression at `transitions.py:268` (the Result is silently dropped today), and a
   green guard asserts the callsite set is **exactly** `{transitions.py}` so a new caller forces review.

## Specs to implement

- [[_slices/slice-c6-lifecycle-event-loss]] — this slice's contract is its `## Test Contract` below.
  At this head the 8 reds are strict-xfail (each reason names the observed defect) and the guard
  passes, so `make tc-coverage SLICE=slice-c6-lifecycle-event-loss` exits 0.
- [[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] — the atomicity dependency (proposed);
  reviewable before any C6 source merge.

## What C6 does NOT close ([[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] — atomicity)

Making the sink durable does NOT make Qdrant + SQLite atomic. `transitions.py:250-268` commits the
Qdrant mutation FIRST, then records; a crash between the mutation commit and even a durable-on-accept
audit still leaves **mutation-without-audit**. Closing that needs a transactional-outbox / two-phase /
idempotent-replay pattern spanning both stores — tracked as the concrete slice
[[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] (an H5/H7 dependency), **reviewable before any C6
source merge**, not this slice. The item-8 AST/callsite contract below proves the caller consumes the new
`Result`, which is the seam C6b builds on. Accepted decision:
[[13-decisions/c6-lifecycle-durability-options]].

## Test Contract

`tests/lifecycle/test_c6_event_loss.py` — deterministic (background daemon effectively disabled via
`flush_every_n=1_000_000`, `flush_every_s=3600`; a `_FailN` fault injector wrapped in a `_write_fault`
context manager that restores the real writer before `close()` so the fault cannot mask an assertion;
all joins bounded by `_JOIN_TIMEOUT` so a regression fails instead of hanging CI). `record()`'s raw
return is read through `_record()`/`_as_result()`, which raise `DefectStillPresent` when the value is
not a `Result` — that is the named red reason today. Metric named before source:
`musubi_lifecycle_event_write_failures_total` (bounded, no labels), asserted via the shared
`default_registry` **rendered exposition** delta — never a private attribute.

The **legacy batch-model tests were removed, not kept** (Yua 2026-07-13): under durable-on-accept the
buffer/flush is not the durability boundary, so `failed-flush-retention`, the `A+B` failing-write race,
and `bounded-backpressure-queue` pass vacuously (they described a current defect, not an acceptance
gate). The eight items below are the acceptance contract.

Reds (8, strict-xfail; each red-proofed to flip to `XPASS(strict)` under the minimal Option-A fix):
1. `test_record_success_is_ok_and_immediately_durable` — healthy record is `Ok` + readable with no
   flush/close.
2. `test_write_failure_is_typed_err_zero_row_metric_and_pii_free_log` — failure is a **concrete
   `LifecycleEventWriteError`** (type + PII-free public fields, not a bare `Err[object]`), zero rows,
   metric exactly one unlabeled series +1, and **exactly one** ERROR record whose **fully rendered form
   (incl. the `exc_info` traceback)** excludes reason/namespace/object_id. The fault raises an exception
   whose message carries those canaries, so a `logger.exception` leak fails — proven both ways: a leaky
   log and an untyped error each leave this red *unflipped*.
3. `test_retry_same_event_id_is_ok_exactly_one_row` — retry of the same `event_id` is `Ok`, exactly one
   row.
4. `test_only_ok_accepted_events_survive_abrupt_crash` — subprocess asserts `returncode==0` then
   `os._exit`; only committed markers survive reopen.
5. `test_concurrent_success_and_failure_no_cross_loss` — 20 concurrent records, one injected failure:
   successes persist exactly once, the failure is `Err`, no cross-loss.
6. `test_sustained_failures_all_err_zero_rows_no_growth` — 1000 sustained failures run in a **bounded
   subprocess** (`_SUSTAINED_TIMEOUT`), so a `record()` that BLOCKS instead of refusing promptly fails
   via `TimeoutExpired` rather than hanging CI; all `Err`, zero rows, `_buffer` empty.
7. `test_close_idempotent_cannot_discard_ok_event` — double `close()` is idempotent and keeps the `Ok`
   event.
8. `test_record_result_is_consumed_not_bare_expression` — AST-parses `transitions.py` and **rejects a
   bare `sink.record(...)` expression**; the `Result` must be consumed/propagated (today it is a bare
   statement at L.268 → red).

Guard (green now + post-fix):
- `test_record_callsite_inventory_is_exactly_reviewed` — mechanical grep proves the `sink.record(`
  callsite set is **exactly** `{transitions.py}` (equality, not subset), so a NEW caller fails the guard
  and forces `Result`-handling review (the C6b boundary).

**Closure at this head:** 1 passed + 8 xfailed; ruff/mypy clean; zero `src/musubi`.
Red-proofed: a temporary minimal Option-A fix (synchronous commit in `record()` returning
`Ok(value=None)` / `Err(error=LifecycleEventWriteError(...))`, `default_registry` counter + PII-free
static ERROR log; `transitions.py` consumes the `Result`) flips ALL 8 reds to `XPASS(strict)` while the
guard stays green; source restored via `git checkout`, nothing committed to `src/`. The two subtle
discriminators were proven to bite: a `logger.exception` leak (canaries in the traceback) and an untyped
string error each leave red #2 *unflipped*, and the fully-correct typed+clean fix flips it.

## Status

**`in-progress`** (2026-07-13) — red contract only (tests + this doc), rebuilt to the ACCEPTED Option A
/ durable-on-accept architecture (Yua 2026-07-13). The source fix is a SEPARATE slice after Yua reviews
this contract; C6b (Qdrant↔SQLite atomicity) is named + linked and must precede any C6 source merge.
Tracking Issue #433. Second reader: Tama or Shiori, requested only after their current lanes clear.

spec-update: slice-c6-lifecycle-event-loss — Option A durable-on-accept red contract for C6 lifecycle
audit-event loss (8 reds + 1 guard, hardened per Yua's proof review): immediate durability on Ok;
typed `LifecycleEventWriteError` + zero-row + one-unlabeled-series-metric + PII-free rendered log (incl.
exc_info traceback) on failure; same-event_id retry exactly-once; crash survival of Ok-accepted events;
concurrent no-cross-loss; bounded-subprocess sustained-failure no-growth; idempotent close; strict AST
red requiring the `Result` be consumed at `transitions.py`; exact-set callsite guard. C6b atomicity
promoted to a concrete linked slice, reviewable before any C6 source merge. Source fix deferred (Yua
2026-07-13).
