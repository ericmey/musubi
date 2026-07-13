"""C6 — lifecycle audit-event loss red contract (tests-only, NO src).

Owner slice: slice-c6-lifecycle-event-loss (Issue #433). Independently reproduced against main
(`src/musubi/lifecycle/events.py`); the finding was confirmed and the durability/observability holes
were widened per Yua's review.

WHAT C6 CLOSES (this contract): the LifecycleEventSink loses no accepted audit event — retention on
flush failure, exactly-once retry, explicit shutdown, durable-on-accept crash survival, bounded
no-silent-loss backpressure, and observable (metric + PII-free log) failure.

WHAT C6 DOES NOT CLOSE: Qdrant-mutation ↔ SQLite-audit ATOMICITY. `transitions.py:250-268` commits the
Qdrant mutation first, then records best-effort; making the SINK durable does NOT make the two stores
atomic (a crash between the mutation commit and a durable-on-accept audit still leaves
mutation-without-audit). That atomicity/outbox gap is tracked SEPARATELY (C6b / H5-H7 dependency),
NOT by this slice. See the design-options memo.

Source observation vs desired contract are separated in the slice doc.

    uv run pytest tests/lifecycle/test_c6_event_loss.py -v
"""

import contextlib
import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import cast

import pytest

from musubi.lifecycle.events import LifecycleEventSink
from musubi.observability.registry import default_registry
from musubi.types.lifecycle_event import LifecycleEvent

#: Named BEFORE source (Yua): the bounded, no-label shared-registry counter the fix must increment on a
#: write failure. Asserted via the default_registry scrape delta, never a private attribute.
_METRIC = "musubi_lifecycle_event_write_failures_total"
_JOIN_TIMEOUT = 5.0  # joins/waits are bounded so a regression FAILS instead of hanging CI


def _mk_sink(db_path: Path) -> LifecycleEventSink:
    """Sink with the background daemon effectively disabled (flush_every_s huge) and no inline flush
    (flush_every_n huge) — so every flush here is explicit and deterministic."""
    return LifecycleEventSink(db_path=db_path, flush_every_n=1_000_000, flush_every_s=3600.0)


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _ev(marker: str) -> LifecycleEvent:
    # `reason` carries a unique marker so read_all() rows are identity-checkable (event_id is auto).
    return LifecycleEvent(
        object_id="0" * 27,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="t",
        reason=marker,
    )


class _FailNThenReal:
    """Wrap a sink's ``_write_batch`` to raise on the first ``n`` calls, then delegate to the real
    write — so a red can prove a transient failure is (or, today, is not) recoverable."""

    def __init__(self, sink: LifecycleEventSink, n: int) -> None:
        self._real = sink._write_batch
        self._remaining = n

    def __call__(self, batch: list[LifecycleEvent]) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("injected transient write failure")
        self._real(batch)


def _metric_total() -> float | None:
    reg = default_registry()
    m = next((x for x in reg._instruments() if getattr(x, "name", None) == _METRIC), None)
    return None if m is None else sum(cast(float, v) for _, v in m.collect())


# --------------------------------------------------------------------------- #
# CONTROL — concurrent record + SUCCESSFUL flush loses/duplicates nothing
# --------------------------------------------------------------------------- #


def test_concurrent_record_and_flush_no_loss_no_dup(tmp_path: Path) -> None:
    """CONTROL (must stay green): the lock-protected append/drain is race-free under a SUCCESSFUL
    write. Insufficient alone (the failing-write race is a separate red) — this only guards the
    happy path."""
    sink = _mk_sink(tmp_path / "e.db")
    try:
        markers = [f"c{i}" for i in range(200)]
        stop = threading.Event()

        def flusher() -> None:
            while not stop.is_set():
                sink.flush()

        t = threading.Thread(target=flusher)
        t.start()
        for m in markers:
            sink.record(_ev(m))
        stop.set()
        t.join(timeout=_JOIN_TIMEOUT)
        assert not t.is_alive(), "flusher thread hung"
        sink.flush()
        got = sorted(e.reason for e in sink.read_all())
        assert got == sorted(markers), f"concurrent record+flush lost/duplicated: {len(got)}/200"
    finally:
        sink.close()


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
            sink.record(_ev(f"a{i}"))
        sink._write_batch = _FailNThenReal(sink, n=1)  # type: ignore[method-assign]
        with contextlib.suppress(Exception):
            sink.flush()
        sink.flush()  # retry
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
        markers = [f"e{i}" for i in range(5)]
        for m in markers:
            sink.record(_ev(m))
        sink._write_batch = _FailNThenReal(sink, n=1)  # type: ignore[method-assign]
        with contextlib.suppress(Exception):
            sink.flush()
        sink.flush()  # retry
        got = [e.reason for e in sink.read_all()]
        if sorted(got) != sorted(markers):
            raise DefectStillPresent(
                f"retry did not yield each event exactly once: {len(got)} rows "
                f"({len(set(got))} distinct) for {len(markers)} events"
            )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 3 — shutdown must not silently discard buffered events
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
        sink.record(_ev(f"s{i}"))
    sink.close()  # documented "final drain" — must persist, not drop
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
# 4 — accepted events must survive an ABRUPT crash (durable-on-accept)
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() only appends to an in-RAM buffer; a process death before the interval/count flush loses the audit AFTER the lifecycle mutation committed — accepted events are not durable-on-accept",
)
def test_accepted_events_survive_abrupt_crash(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    prog = textwrap.dedent(
        f"""
        import os
        from musubi.lifecycle.events import LifecycleEventSink
        from musubi.types.lifecycle_event import LifecycleEvent
        s = LifecycleEventSink(db_path={str(db)!r}, flush_every_n=1_000_000, flush_every_s=3600.0)
        for i in range(3):
            s.record(LifecycleEvent(object_id="0"*27, object_type="episodic",
                namespace="eric/claude-code/episodic", from_state="provisional",
                to_state="matured", actor="t", reason=f"k{{i}}"))
        os._exit(0)   # abrupt: no close(), no flush(), no finalizers
        """
    )
    subprocess.run([sys.executable, "-c", prog], capture_output=True, timeout=30, check=False)
    reopened = _mk_sink(db)
    try:
        persisted = len(reopened.read_all())
        if persisted != 3:
            raise DefectStillPresent(
                f"accepted events did not survive an abrupt crash: {persisted}/3 durable in the db "
                "after os._exit without close()/flush()"
            )
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# 5 — a failing write racing a concurrent record must preserve BOTH batches
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="while batch A is outside the lock and its write fails, a concurrent record() appends batch B; A is discarded (buffer already cleared) so a retry recovers only B — order/cardinality of A+B is not preserved",
)
def test_failing_write_race_preserves_a_and_b(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        real_write = sink._write_batch
        in_write = threading.Event()
        release = threading.Event()

        def racing_failing_write(batch: list[LifecycleEvent]) -> None:
            in_write.set()  # A's write has started (buffer already cleared in current code)
            release.wait(
                timeout=_JOIN_TIMEOUT
            )  # hold inside the failure window until B is appended
            raise RuntimeError("A's write fails while B is being appended")

        for i in range(3):
            sink.record(_ev(f"A{i}"))  # batch A
        sink._write_batch = racing_failing_write  # type: ignore[method-assign]

        errs: list[BaseException] = []

        def do_flush() -> None:
            try:
                sink.flush()
            except BaseException as e:
                errs.append(e)

        tf = threading.Thread(target=do_flush)
        tf.start()
        assert in_write.wait(timeout=_JOIN_TIMEOUT), "flush never entered the write window"
        for i in range(2):
            sink.record(_ev(f"B{i}"))  # batch B, concurrent with A's in-flight (failing) write
        release.set()
        tf.join(timeout=_JOIN_TIMEOUT)
        assert not tf.is_alive(), "flush thread hung"

        sink._write_batch = real_write  # type: ignore[method-assign]
        sink.flush()  # recovery + retry
        got = sorted(e.reason for e in sink.read_all())
        expected = sorted([f"A{i}" for i in range(3)] + [f"B{i}" for i in range(2)])
        if got != expected:
            raise DefectStillPresent(
                f"failing-write race lost/duplicated A+B: got {got} expected {expected}"
            )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 6 — under sustained failure: BOUNDED + no-silent-loss backpressure (not drop, not block-forever)
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() accepts unboundedly into an in-RAM buffer and never applies backpressure — under sustained write failure it neither bounds memory nor refuses; the required policy is bounded retention with backpressure (never silent drop, never block forever)",
)
def test_sustained_failure_bounded_backpressure_no_silent_loss(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        real_write = sink._write_batch
        sink._write_batch = _FailNThenReal(sink, n=1_000_000)  # type: ignore[method-assign]
        accepted, rejected = 0, 0
        for i in range(1000):
            try:
                sink.record(_ev(f"x{i}"))  # under a durable-on-accept fix this write can fail
                accepted += 1
            except Exception:
                rejected += 1  # backpressure: refused rather than silently retained/lost
            with contextlib.suppress(Exception):
                sink.flush()
        # BOUNDED + backpressure: the sink must not silently accept 1000 events with none durable.
        if rejected == 0:
            raise DefectStillPresent(
                f"no backpressure under sustained failure: record() accepted {accepted}/1000 with no "
                "refusal — unbounded silent accept-then-lose (needs bounded retention + backpressure)"
            )
        # NO SILENT LOSS: every ACCEPTED event must be durable once the backend recovers.
        sink._write_batch = real_write  # type: ignore[method-assign]
        sink.flush()
        persisted = len(sink.read_all())
        if persisted < accepted:
            raise DefectStillPresent(
                f"silent loss: {accepted} accepted but only {persisted} durable after recovery"
            )
    finally:
        sink.close()


# --------------------------------------------------------------------------- #
# 7 — a swallowed flush failure must be OBSERVABLE via the shared registry + a PII-free log
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="_flush_loop does `contextlib.suppress(Exception): self.flush()` with NO telemetry — the shared default_registry counter musubi_lifecycle_event_write_failures_total does not exist and no error is logged, so a swallowed failure is indistinguishable from success",
)
def test_swallowed_flush_failure_observable_metric_and_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    before = _metric_total()
    sink = _mk_sink(tmp_path / "e.db")
    try:
        sink.record(_ev("obs"))
        sink._write_batch = _FailNThenReal(sink, n=10)  # type: ignore[method-assign]
        # A write failure must be counted regardless of WHERE the fix surfaces it — at flush time
        # (batch-flush design) or at record time (durable-on-accept design). Both paths are exercised
        # under suppression, exactly as _flush_loop swallows.
        with caplog.at_level(logging.ERROR):
            with contextlib.suppress(Exception):
                sink.record(_ev("more"))
            with contextlib.suppress(Exception):
                sink.flush()
        after = _metric_total()
        if before is None or after is None or after <= before:
            raise DefectStillPresent(
                f"a swallowed write failure did not increment the shared {_METRIC} "
                f"(before={before}, after={after})"
            )
        errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        if not errs:
            raise DefectStillPresent("swallowed flush failure produced no error log")
        if any("eric/claude-code/episodic" in m or "obs" in m or "more" in m for m in errs):
            raise DefectStillPresent("error log leaked event body/namespace (must be PII-free)")
    finally:
        sink.close()
