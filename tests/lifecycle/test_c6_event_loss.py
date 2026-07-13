"""C6 — lifecycle audit-event loss red contract (tests-only, NO src).

Owner slice: slice-c6-lifecycle-event-loss. Independently reproduced against main (events.py) — the
prose was confirmed AND two extra loss paths were found (close() discard; total silence).

SOURCE OBSERVATION (what the code does today, `src/musubi/lifecycle/events.py`):
- `flush()` reassigns `self._buffer = []` (l.115) BEFORE `self._write_batch(pending)` (l.118). If the
  write raises, `pending` is a discarded local and the buffer is already empty → the batch is LOST,
  with no path to retry. The comment at l.158-160 claiming "the buffer is preserved … will retry" is
  FALSE.
- `_write_batch` re-raises after ROLLBACK (l.211), so the failure DOES reach `flush()` — which has
  already cleared.
- `_flush_loop` wraps `flush()` in `contextlib.suppress(Exception)` (l.161) with ZERO telemetry, so a
  background write failure loses events SILENTLY.
- `close()` final-drains via `flush()` (l.147) but `_write_batch` early-returns on `self._closed`
  (l.197-198) → the buffered events are discarded on shutdown without being written.
- `transitions.py:250-268`: the Qdrant mutation COMMITS first, then `sink.record(event)` runs with no
  try/except — audit is best-effort AFTER a committed mutation, and most records only append (buffer
  < flush_every_n=100), so the silent background-flush path is the common loss case.

DESIRED CONTRACT (what must hold; each red is strict-xfail against today):
1. a failed flush preserves every event for retry;
2. a successful retry writes each event exactly once (event_id PK + INSERT OR REPLACE make this
   achievable once events are retained);
3. concurrent record/flush loses or duplicates nothing (CONTROL — the lock design must stay sound);
4. shutdown is explicit — close() must not silently discard buffered events;
5. under sustained write failure the retained buffer is BOUNDED with a NAMED backpressure policy
   (the retention fix must not invent unbounded growth);
6. a swallowed flush failure must be OBSERVABLE (telemetry/counter), never silent success.

    uv run pytest tests/lifecycle/test_c6_event_loss.py -v
"""

import contextlib
import threading
from pathlib import Path

import pytest

from musubi.lifecycle.events import LifecycleEventSink
from musubi.types.lifecycle_event import LifecycleEvent


def _mk_sink(db_path: Path) -> LifecycleEventSink:
    """A sink with the background daemon effectively disabled (flush_every_s huge) and no inline flush
    (flush_every_n huge) — so every flush in these tests is explicit and deterministic."""
    return LifecycleEventSink(db_path=db_path, flush_every_n=1_000_000, flush_every_s=3600.0)


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _ev(i: int) -> LifecycleEvent:
    return LifecycleEvent(
        object_id="0" * 27,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="t",
        reason=f"r{i}",
    )


class _FailNThenReal:
    """Wrap a sink's ``_write_batch`` to raise on the first ``n`` calls, then delegate to the real
    write — so a red can prove a transient failure is recoverable (or, today, is not)."""

    def __init__(self, sink: LifecycleEventSink, n: int) -> None:
        self._real = sink._write_batch
        self._remaining = n
        self.calls = 0

    def __call__(self, batch: list[LifecycleEvent]) -> None:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("injected transient write failure")
        self._real(batch)


# --------------------------------------------------------------------------- #
# 1 — a failed flush must preserve every event for retry
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="flush() clears self._buffer BEFORE _write_batch, so a write failure discards the batch — a subsequent retry recovers nothing",
)
def test_failed_flush_preserves_events_for_retry(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        for i in range(3):
            sink.record(_ev(i))
        sink._write_batch = _FailNThenReal(sink, n=1)  # type: ignore[method-assign]
        with contextlib.suppress(Exception):
            sink.flush()  # write fails; today this also empties the buffer
        # retry: the events must still be recoverable and land on a successful retry
        sink.flush()
        persisted = len(sink.read_all())
        if persisted != 3:
            raise DefectStillPresent(
                f"a failed flush lost the batch: {persisted}/3 events persisted after retry"
            )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 2 — a successful retry writes each event exactly once
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="retry is impossible today (events discarded on the failed flush), so exactly-once-on-retry cannot hold",
)
def test_successful_retry_writes_each_event_exactly_once(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        events = [_ev(i) for i in range(5)]
        for ev in events:
            sink.record(ev)
        sink._write_batch = _FailNThenReal(sink, n=1)  # type: ignore[method-assign]
        with contextlib.suppress(Exception):
            sink.flush()
        sink.flush()  # retry
        got_ids = [e.event_id for e in sink.read_all()]
        expected_ids = {e.event_id for e in events}
        if sorted(got_ids) != sorted(expected_ids):
            raise DefectStillPresent(
                f"retry did not yield each event exactly once: got {len(got_ids)} rows "
                f"({len(set(got_ids))} distinct) for {len(expected_ids)} events"
            )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 3 — CONTROL: concurrent record + successful flush loses/duplicates nothing
# --------------------------------------------------------------------------- #


def test_concurrent_record_and_flush_no_loss_no_dup(tmp_path: Path) -> None:
    """CONTROL (must stay green): the lock-protected append/drain is race-free under a successful
    write. Guards that a retention fix does not introduce a concurrency loss/dup."""
    sink = _mk_sink(tmp_path / "e.db")
    try:
        n = 200
        events = [_ev(i) for i in range(n)]
        stop = threading.Event()

        def flusher() -> None:
            while not stop.is_set():
                sink.flush()

        t = threading.Thread(target=flusher)
        t.start()
        for ev in events:
            sink.record(ev)
        stop.set()
        t.join()
        sink.flush()  # final drain of anything the flusher left
        got = [e.event_id for e in sink.read_all()]
        assert sorted(got) == sorted(e.event_id for e in events), (
            f"concurrent record+flush lost/duplicated: {len(got)} rows "
            f"({len(set(got))} distinct) for {n} events"
        )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 4 — shutdown must not silently discard buffered events
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="close() final-drains via flush(), but _write_batch early-returns on self._closed — the buffered events are discarded on shutdown without being written",
)
def test_close_does_not_silently_discard_buffered_events(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    sink = _mk_sink(db)
    for i in range(3):
        sink.record(_ev(i))
    sink.close()  # the documented "final drain" — must persist, not drop
    reopened = _mk_sink(db)
    try:
        persisted = len(reopened.read_all())
        if persisted != 3:
            raise DefectStillPresent(
                f"close() silently discarded buffered events: {persisted}/3 persisted after shutdown"
            )
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# 5 — under sustained write failure the retained buffer must be BOUNDED + policy NAMED
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="no retention+bound policy exists: events are lost on failure (so nothing is retained to bound), and no backpressure policy is named — the fix must retain for retry AND cap memory with a named policy",
)
def test_sustained_failure_retains_bounded_not_lost(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        real_write = sink._write_batch  # capture the real writer to restore on "recovery"
        sink._write_batch = _FailNThenReal(sink, n=1_000_000)  # type: ignore[method-assign]
        for i in range(50):
            sink.record(_ev(i))
            with contextlib.suppress(Exception):
                sink.flush()  # every flush fails throughout the outage
        # backend recovers → a retry must persist the retained events (contract). Today they are gone.
        sink._write_batch = real_write  # type: ignore[method-assign]
        sink.flush()
        persisted = len(sink.read_all())
        if persisted != 50:
            raise DefectStillPresent(
                f"sustained-failure events were lost, not retained for retry: {persisted}/50 "
                "persisted after recovery — and no bounded/backpressure policy is named"
            )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 6 — a swallowed flush failure must be OBSERVABLE, never silent
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="_flush_loop does `contextlib.suppress(Exception): self.flush()` with NO telemetry — a swallowed background flush failure leaves no observable signal (no counter/log/state)",
)
def test_swallowed_flush_failure_is_observable(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        sink.record(_ev(0))
        sink._write_batch = _FailNThenReal(sink, n=1)  # type: ignore[method-assign]
        # exactly what the background loop does (events.py:161):
        with contextlib.suppress(Exception):
            sink.flush()
        signal = getattr(sink, "flush_failure_count", None)
        if not signal:
            raise DefectStillPresent(
                "a swallowed flush failure produced NO observable signal (no flush_failure_count / "
                "telemetry / recorded error) — failure is indistinguishable from success"
            )
    finally:
        sink.close()
