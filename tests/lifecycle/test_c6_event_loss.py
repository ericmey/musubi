"""C6 — lifecycle audit durability red contract, OPTION A / durable-on-accept (tests-only, NO src).

Owner slice: slice-c6-lifecycle-event-loss (Issue #433). Architecture ACCEPTED by Yua: durable-on-accept.
`record()` synchronously COMMITS the event to SQLite and returns `Result[None, LifecycleEventWriteError]`
(per AGENTS.md l.105 — a mutation at a module boundary returns a Result, never a raised/suppressed
exception). An event is "accepted" ONLY after COMMIT. The RAM buffer + background flusher are removed as
a durability mechanism — a failed write is refused immediately, so there is NO retry queue and NO
backpressure cap (nothing accumulates in memory).

WHAT C6 CLOSES: no accepted (committed) audit event is ever lost — success is immediately durable;
failure is a refused `Err` with zero rows + an observable metric/log; retry is idempotent; crash and
close cannot lose an Ok event.

WHAT C6 DOES NOT CLOSE (C6b, named + linked before C6 source merge): Qdrant↔SQLite ATOMICITY.
`transitions.py:250-268` commits the Qdrant mutation FIRST, then records — durable-on-accept does NOT
make the two stores atomic. Tracked as C6b / an H5-H7 dependency. See the accepted Option-A memo.

Current code returns `None` from `record()` and buffers in RAM, so every red is strict-xfail here.

    uv run pytest tests/lifecycle/test_c6_event_loss.py -v
"""

import contextlib
import logging
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from musubi.lifecycle.events import LifecycleEventSink
from musubi.observability.registry import default_registry, render_text_format
from musubi.types.common import Err, Ok
from musubi.types.lifecycle_event import LifecycleEvent

_Result = "Ok[None] | Err[object]"

#: Named before source (Yua): a BOUNDED, NO-LABEL shared-registry counter; the fix increments it +1 per
#: injected write failure. Asserted via the RENDERED exposition (exactly one unlabeled series).
_METRIC = "musubi_lifecycle_event_write_failures_total"
_JOIN_TIMEOUT = 5.0  # bounded joins/waits so a regression FAILS instead of hanging CI


def _mk_sink(db_path: Path) -> LifecycleEventSink:
    return LifecycleEventSink(db_path=db_path, flush_every_n=1_000_000, flush_every_s=3600.0)


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _ev(marker: str, object_id: str = "0" * 27) -> LifecycleEvent:
    return LifecycleEvent(
        object_id=object_id,
        object_type="episodic",
        namespace="eric/claude-code/episodic",
        from_state="provisional",
        to_state="matured",
        actor="t",
        reason=marker,
    )


class _FailN:
    """Inject write failure: raise on the first ``n`` calls to the sink's write, then delegate."""

    def __init__(self, sink: LifecycleEventSink, n: int) -> None:
        self._real = sink._write_batch
        self._remaining = n

    def __call__(self, batch: list[LifecycleEvent]) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise RuntimeError("injected write failure")
        self._real(batch)


@contextlib.contextmanager
def _write_fault(sink: LifecycleEventSink, n: int) -> Iterator[None]:
    """Inject a write fault for the body, then RESTORE the real writer — so the ``finally: close()``
    (which flushes) does not re-hit the fault and mask the assertion."""
    real = sink._write_batch
    sink._write_batch = _FailN(sink, n)  # type: ignore[method-assign]
    try:
        yield
    finally:
        sink._write_batch = real  # type: ignore[method-assign]


def _rendered_metric_lines() -> list[str]:
    text = render_text_format(default_registry())
    return [ln for ln in text.splitlines() if ln.startswith(_METRIC) and not ln.startswith("#")]


def _metric_value() -> float | None:
    reg = default_registry()
    m = next((x for x in reg._instruments() if getattr(x, "name", None) == _METRIC), None)
    return None if m is None else sum(cast(float, v) for _, v in m.collect())


def _as_result(value: object) -> "Ok[None] | Err[object]":
    if not isinstance(value, (Ok, Err)):
        raise DefectStillPresent(
            f"record() must return Result[None, LifecycleEventWriteError], got {type(value).__name__}"
        )
    return cast("Ok[None] | Err[object]", value)


def _record(sink: LifecycleEventSink, ev: LifecycleEvent) -> object:
    # record() is typed `-> None` today; the contract requires a Result. Read the raw return.
    return cast(object, sink.record(ev))


# 1 — success => Ok + immediately durable (no flush/close) ------------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() returns None and only buffers in RAM — it is neither a Result nor durable-on-accept",
)
def test_record_success_is_ok_and_immediately_durable(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        res = _as_result(_record(sink, _ev("ok1")))
        if not res.is_ok():
            raise DefectStillPresent(f"healthy record() must be Ok, got Err: {res}")
        rows = [e.reason for e in sink.read_all()]  # NO flush(), NO close()
        if rows != ["ok1"]:
            raise DefectStillPresent(f"record() was not immediately durable: rows={rows}")
    finally:
        sink.close()


# 2 — write failure => Err + zero row + exact metric + PII-free log ---------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="a write failure today buffers (no Err), persists nothing, and is suppressed with no metric/log — indistinguishable from success",
)
def test_write_failure_is_err_zero_row_metric_and_pii_free_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    before = _metric_value() or 0.0
    sink = _mk_sink(tmp_path / "e.db")
    try:
        canary = "PiiReasonCanary"
        oid = "objidcanary0000000000000000"[:27]
        with _write_fault(sink, n=10), caplog.at_level(logging.ERROR):
            res = _as_result(_record(sink, _ev(canary, object_id=oid)))
            if not res.is_err():
                raise DefectStillPresent("a failed write must return Err, not Ok/None")
            if sink.read_all():
                raise DefectStillPresent("a failed write must persist ZERO rows")
            lines = _rendered_metric_lines()
            if len(lines) != 1 or "{" in lines[0]:
                raise DefectStillPresent(
                    f"metric must be exactly one UNLABELED series; got {lines}"
                )
            if (_metric_value() or 0.0) - before != 1.0:
                raise DefectStillPresent("metric must increment by exactly +1 per failure")
            errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
            if not errs:
                raise DefectStillPresent("a write failure must log an ERROR")
            if any(canary in m or oid in m or "eric/claude-code/episodic" in m for m in errs):
                raise DefectStillPresent("log leaked namespace/reason/object_id (must be PII-free)")
    finally:
        sink.close()


# 3 — retry the SAME event_id after a transient failure => Ok + exactly one row #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() is not a Result and not durable-on-accept, so a same-event_id retry-to-exactly-one-row cannot hold",
)
def test_retry_same_event_id_is_ok_exactly_one_row(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        ev = _ev("retry")  # SAME event_id object reused across both attempts
        with _write_fault(sink, n=1):
            first = _as_result(_record(sink, ev))
            if not first.is_err():
                raise DefectStillPresent("the transiently-failing first attempt must be Err")
        second = _as_result(_record(sink, ev))  # retry same event_id, writer restored
        if not second.is_ok():
            raise DefectStillPresent("the retry must be Ok")
        rows = [e.reason for e in sink.read_all()]
        if rows != ["retry"]:
            raise DefectStillPresent(f"retry must yield exactly one row (event_id PK); got {rows}")
    finally:
        sink.close()


# 4 — subprocess: returncode 0 then os._exit; only Ok-accepted markers survive #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() only appends to RAM; a process death before a flush loses every accepted event — not durable-on-accept",
)
def test_only_ok_accepted_events_survive_abrupt_crash(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    prog = textwrap.dedent(
        f"""
        import os
        from musubi.types.common import Ok
        from musubi.lifecycle.events import LifecycleEventSink
        from musubi.types.lifecycle_event import LifecycleEvent
        s = LifecycleEventSink(db_path={str(db)!r}, flush_every_n=1_000_000, flush_every_s=3600.0)
        oks = 0
        for i in range(3):
            r = s.record(LifecycleEvent(object_id="0"*27, object_type="episodic",
                namespace="eric/claude-code/episodic", from_state="provisional",
                to_state="matured", actor="t", reason=f"ok{{i}}"))
            if isinstance(r, Ok):
                oks += 1
        os._exit(0 if oks == 3 else 3)   # abrupt: no close(), no flush()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", prog], capture_output=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        raise DefectStillPresent(
            f"record() did not return Ok for accepted events (subprocess rc={proc.returncode})"
        )
    reopened = _mk_sink(db)
    try:
        survivors = sorted(e.reason for e in reopened.read_all())
        if survivors != ["ok0", "ok1", "ok2"]:
            raise DefectStillPresent(
                f"only Ok-accepted events must survive an abrupt crash; got {survivors}"
            )
    finally:
        reopened.close()


# 5 — concurrent successes + one failure: successes persist once, failure Errs, no cross-loss #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() is not durable-on-accept and returns no Result, so a concurrent success/failure mix cannot guarantee exactly-once successes + an Err failure with no cross-loss",
)
def test_concurrent_success_and_failure_no_cross_loss(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    real_write = sink._write_batch
    try:
        results: dict[str, object] = {}
        lock = threading.Lock()
        fail_marker = "w0"

        def maybe_failing_write(batch: list[LifecycleEvent]) -> None:
            if any(e.reason == fail_marker for e in batch):
                raise RuntimeError("injected failure for w0")
            real_write(batch)

        sink._write_batch = maybe_failing_write  # type: ignore[method-assign]

        def worker(marker: str) -> None:
            raw = _record(sink, _ev(marker))  # capture raw; do not raise in the worker thread
            with lock:
                results[marker] = raw

        markers = [f"w{i}" for i in range(20)]
        threads = [threading.Thread(target=worker, args=(m,)) for m in markers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT)
            assert not t.is_alive(), "a record() worker hung"

        parsed = {m: _as_result(results[m]) for m in markers}  # raises here if any is not a Result
        if not parsed[fail_marker].is_err():
            raise DefectStillPresent("the failing concurrent record must return Err")
        durable = sorted(e.reason for e in sink.read_all())
        expected = sorted(m for m in markers if m != fail_marker)
        if durable != expected:
            raise DefectStillPresent(
                f"concurrent successes must persist exactly once with no cross-loss; "
                f"durable={durable} expected={expected}"
            )
    finally:
        sink._write_batch = real_write  # type: ignore[method-assign]
        sink.close()


# 6 — sustained 1000 failures: all Err promptly, zero rows, no in-memory growth #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="record() accepts into an unbounded RAM buffer and returns None; under sustained failure it neither refuses (Err) nor stays bounded",
)
def test_sustained_failures_all_err_zero_rows_no_growth(tmp_path: Path) -> None:
    sink = _mk_sink(tmp_path / "e.db")
    try:
        with _write_fault(sink, n=10_000):
            errs = 0
            for i in range(1000):
                if _as_result(_record(sink, _ev(f"x{i}"))).is_err():
                    errs += 1
            if errs != 1000:
                raise DefectStillPresent(
                    f"every record under sustained failure must be Err; got {errs}/1000"
                )
            if sink.read_all():
                raise DefectStillPresent("sustained failures must persist ZERO rows")
            buffered = len(getattr(sink, "_buffer", []))
            if buffered != 0:
                raise DefectStillPresent(
                    f"failed records must not accumulate in memory; buffered={buffered}"
                )
    finally:
        sink.close()


# 7 — close is idempotent and cannot discard an already-Ok event -------------- #


@pytest.mark.xfail(
    raises=DefectStillPresent,
    strict=True,
    reason="an Ok event is not durable-on-accept today, and close() drops the buffer via the _closed early-return — a committed event could be lost / close is not proven idempotent",
)
def test_close_idempotent_cannot_discard_ok_event(tmp_path: Path) -> None:
    db = tmp_path / "e.db"
    sink = _mk_sink(db)
    res = _as_result(_record(sink, _ev("kept")))
    if not res.is_ok():
        raise DefectStillPresent("record() must return Ok for the accepted event")
    sink.close()
    sink.close()  # idempotent — must not raise or discard
    reopened = _mk_sink(db)
    try:
        rows = [e.reason for e in reopened.read_all()]
        if rows != ["kept"]:
            raise DefectStillPresent(f"close() must not discard an Ok event; rows={rows}")
    finally:
        reopened.close()


# 8 — CONTROL/guard: mechanical record() callsite inventory (no caller may ignore the Result) #


def test_record_callsite_inventory_is_reviewed() -> None:
    """Item 8 (guard, green now): every LifecycleEventSink.record() callsite must handle the new
    Result[None, LifecycleEventWriteError]. This inventories the callsites so a NEW one forces review;
    the ONLY current callsite is transitions.py, whose mutation-first ordering is the C6b ticket."""
    src = Path(__file__).resolve().parents[2] / "src" / "musubi"
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", r"\.record(", str(src)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    callsites = [ln for ln in out.splitlines() if "sink.record(" in ln]
    seen = {Path(ln.split(":")[0]).name for ln in callsites}
    reviewed = {"transitions.py"}  # the C6 src migration must make each handle the Err
    assert seen <= reviewed, (
        f"un-reviewed record() callsite(s) {seen - reviewed} — each must handle "
        "Result[None, LifecycleEventWriteError] (see the C6 slice inventory)"
    )
