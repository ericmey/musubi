---
title: "Slice: C6 lifecycle audit-event loss — durable-on-accept implementation"
slice_id: slice-c6-lifecycle-event-loss
section: _slices
type: slice
status: in-progress
owner: yua
phase: "Lifecycle-audit 2026-07-14 — C6 durable-on-accept implementation successor"
tags: [section/slices, status/in-progress, type/slice, lifecycle, audit, durability]
updated: 2026-07-14
reviewed: false
depends-on: []
blocks: []
issue: 433
---

# Slice: C6 lifecycle audit-event loss — durable-on-accept implementation

Implementation successor for the accepted C6 audit-event-loss red contract (Issue #433). The original
tests-first evidence remains in history at `c7b95da`; this successor adds the reviewed synchronous
durable-on-accept source and removes each strict-xfail decorator atomically with the behavior it fixes.
Eric's finding was independently reproduced against `main`, and two additional loss paths were found.

## Historical source observation (what the accepted pre-source snapshot did)

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

## Implemented contract — ACCEPTED Option A / durable-on-accept

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
7. `close()` is **idempotent**, cannot discard an already-`Ok` event, and serializes with a concurrent
   `record()` so the race returns either a committed `Ok` or a refused `Err`, never `Ok` without a row;
8. the returned `Result` **must be consumed** at every callsite — a strict AST red rejects the bare
   `sink.record(event)` expression at `transitions.py:268` in the pre-source snapshot, and a
   green guard asserts the callsite set is **exactly** `{transitions.py}` so a new caller forces review.

## Specs to implement

- [[_slices/slice-c6-lifecycle-event-loss]] — this slice's contract is its `## Test Contract` below.
  The accepted pre-source snapshot had 9 strict-xfails and one passing guard; this implementation
  successor makes all 10 ordinary tests pass.
- C6b / Issue #437 — the separate Qdrant↔SQLite atomicity track.

## What C6 does NOT close (C6b / Issue #437 — atomicity)

Making the sink durable does NOT make Qdrant + SQLite atomic. `transitions.py:250-268` commits the
Qdrant mutation FIRST, then records; a crash between the mutation commit and even a durable-on-accept
audit still leaves **mutation-without-audit**. Closing that needs a transactional-outbox / two-phase /
idempotent-replay pattern spanning both stores — tracked separately as C6b / Issue #437 (an H5/H7
dependency), not this slice. The item-8 AST/callsite contract below proves the caller consumes the new
`Result`, which is the seam C6b builds on. Accepted decision:
[[13-decisions/c6-lifecycle-durability-options]].

## Test Contract

`tests/lifecycle/test_c6_event_loss.py` — deterministic (background daemon effectively disabled via
`flush_every_n=1_000_000`, `flush_every_s=3600`; a `_FailN` fault injector wrapped in a `_write_fault`
context manager that restores the real writer before `close()` so the fault cannot mask an assertion;
all joins bounded by `_JOIN_TIMEOUT` so a regression fails instead of hanging CI). `record()`'s raw
return is read through `_record()`/`_as_result()`, which raise `DefectStillPresent` when the value is
not a `Result` — that was the named pre-source red reason. Metric named before source:
`musubi_lifecycle_event_write_failures_total` (bounded, no labels), asserted via the shared
`default_registry` **rendered exposition** delta — never a private attribute.

The **legacy batch-model tests were removed, not kept** (Yua 2026-07-13): under durable-on-accept the
buffer/flush is not the durability boundary, so `failed-flush-retention`, the `A+B` failing-write race,
and `bounded-backpressure-queue` pass vacuously (they described a current defect, not an acceptance
gate). The nine items below are the acceptance contract.

Acceptance tests (9; strict-xfail in the pre-source snapshot, ordinary passing tests with the fix):
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
7. `test_close_idempotent_cannot_discard_ok_event` — double `close()` is idempotent, keeps the `Ok`
   event, and a bounded record/close race never returns `Ok` without the committed row.
8. `test_record_result_is_consumed_not_bare_expression` — AST-parses `transitions.py` and **rejects a
   bare `sink.record(...)` expression**; the `Result` must be consumed/propagated (the pre-source
   snapshot dropped it as a bare statement).
9. `test_transition_callsite_injects_sink_err_yields_caller_err` — injects a refused sink write after
   the Qdrant state mutation, proves the mutation is already visible, and requires `transition()` to
   return `Err(TransitionError(code="lifecycle_event_write_failed"))` rather than falsely reporting `Ok`.

Guard (green now + post-fix):
- `test_record_callsite_inventory_is_exactly_reviewed` — mechanical grep proves the `sink.record(`
  callsite set is **exactly** `{transitions.py}` (equality, not subset), so a NEW caller fails the guard
  and forces `Result`-handling review (the C6b boundary).

**Pre-source proof:** 1 guard passed + 9 strict-xfailed for their named reasons. **Implementation
closure at this head:** all 9 acceptance tests + the inventory guard pass. The source commits each event
synchronously before returning `Ok`, refuses failures as the exact typed error, increments the one
unlabeled shared counter, emits one static PII-free ERROR, removes the RAM buffer/background flusher,
and makes the sole caller propagate the refused write honestly. The two subtle discriminators remain
load-bearing: a `logger.exception` leak and an error object with an extra field both fail test 2.

## Status

**`in-progress`** (2026-07-14) — Option-A source implemented on a clean main-based promotion branch and
awaiting exact-head independent review. C6b (Qdrant↔SQLite atomicity) remains separately tracked as
Issue #437. Tracking Issue #433. Second readers: Tama and Shiori.

spec-update: slice-c6-lifecycle-event-loss — Option A durable-on-accept red contract for C6 lifecycle
audit-event loss (9 acceptance tests + 1 guard, hardened per Yua's proof review): immediate durability on Ok;
typed `LifecycleEventWriteError` + zero-row + one-unlabeled-series-metric + PII-free rendered log (incl.
exc_info traceback) on failure; same-event_id retry exactly-once; crash survival of Ok-accepted events;
concurrent no-cross-loss; bounded-subprocess sustained-failure no-growth; idempotent close; strict AST
test requiring the `Result` be consumed at `transitions.py`; behavioral propagation of a refused write;
exact-set callsite guard. C6b atomicity remains separately tracked as Issue #437.
