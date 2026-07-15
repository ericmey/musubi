"""C6 — lifecycle audit durability, OPTION A / durable-on-accept.

Owner slice: slice-c6-lifecycle-event-loss (Issue #433). Architecture ACCEPTED by Yua: durable-on-accept.
`record()` synchronously COMMITS the event to SQLite and returns `Result[None, LifecycleEventWriteError]`
(per AGENTS.md l.105 — a mutation at a module boundary returns a Result, never a raised/suppressed
exception). An event is "accepted" ONLY after COMMIT. The RAM buffer + background flusher are removed as
a durability mechanism — a failed write is refused immediately, so there is NO retry queue and NO
backpressure cap (nothing accumulates in memory).

WHAT C6 CLOSES: no accepted (committed) audit event is ever lost — success is immediately durable;
failure is a refused `Err` (a concrete, PII-free `LifecycleEventWriteError`) with zero rows + an
observable metric + a PII-free ERROR log; retry is idempotent; crash and close cannot lose an Ok event.

WHAT C6 DOES NOT CLOSE (C6b — a concrete, linked slice, precondition of any C6 source merge):
Qdrant↔SQLite ATOMICITY. `transitions.py:250-268` commits the Qdrant mutation FIRST, then records —
durable-on-accept does NOT make the two stores atomic. See slice-c6b-lifecycle-qdrant-sqlite-atomicity.

Discriminators hardened per Yua's proof review (2026-07-13):
- the PII red injects an exception whose MESSAGE carries the reason/namespace/object_id canaries and
  scans the FULLY RENDERED log (incl. the exc_info traceback), so `logger.exception` leaking would FAIL;
- the failure red pins the concrete error TYPE and an EXACT bounded public shape
  ({"code": "lifecycle_event_write_failed"} only), so a same-type error that grows an extra PII-capable
  field fails — not just `Err[object]`;
- the sustained-failure red runs behind a bounded subprocess so a blocking `record()` fails, not hangs;
- the callsite item is split: a green guard asserts EXACTLY one reviewed callsite, and a strict red
  AST-rejects the bare `sink.record(event)` expression (the Result must be consumed, not dropped).

The accepted pre-source snapshot proved 9 strict-xfail reds + 1 green guard. This implementation
successor removes those decorators atomically with the source fix; all 10 tests are ordinary positives.

    uv run pytest tests/lifecycle/test_c6_event_loss.py -v
"""

import ast
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
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.events import LifecycleEventSink
from musubi.lifecycle.transitions import transition
from musubi.observability.registry import default_registry, render_text_format
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.lifecycle_event import LifecycleEvent

#: Named before source (Yua): a BOUNDED, NO-LABEL shared-registry counter; the fix increments it +1 per
#: injected write failure. Asserted via the RENDERED exposition (exactly one unlabeled series).
_METRIC = "musubi_lifecycle_event_write_failures_total"
_JOIN_TIMEOUT = 5.0  # bounded joins/waits so a regression FAILS instead of hanging CI
_SUSTAINED_TIMEOUT = (
    30.0  # bounded subprocess wall-clock so a BLOCKING record() fails, never hangs CI
)

#: PII canaries — a leaky log or error object that echoes the event content will surface one of these.
_REASON_CANARY = "reasonCANARYzz"
_NS_CANARY = "nscanary/pcanary/episodic"  # valid tenant/presence/plane, distinctive
_OID_CANARY = "oidcanary000000000000000000"  # 27-char base62 KSUID


def _mk_sink(db_path: Path) -> LifecycleEventSink:
    return LifecycleEventSink(db_path=db_path, flush_every_n=1_000_000, flush_every_s=3600.0)


class DefectStillPresent(Exception):
    """Raised when the current code still exhibits the contract-forbidden defect."""


def _ev(
    marker: str,
    object_id: str = "0" * 27,
    namespace: str = "eric/claude-code/episodic",
) -> LifecycleEvent:
    return LifecycleEvent(
        object_id=object_id,
        object_type="episodic",
        namespace=namespace,
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


class _LeakyWrite:
    """Raise an exception whose MESSAGE embeds the PII canaries — so a fix that logs the exception
    (``logger.exception`` / ``exc_info=True``) or interpolates the event LEAKS and the red catches it."""

    def __call__(self, batch: list[LifecycleEvent]) -> None:
        raise RuntimeError(
            f"disk write failed [{_REASON_CANARY}] ns={_NS_CANARY} oid={_OID_CANARY}"
        )


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


def _rendered_error_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Fully render each ERROR record INCLUDING the exc_info traceback — so a leaked exception message
    (via ``logger.exception``) is visible to the PII scan, not just the format string."""
    fmt = (
        logging.Formatter()
    )  # default "%(message)s" + appended traceback when record.exc_info is set
    return [fmt.format(r) for r in caplog.records if r.levelno >= logging.ERROR]


def _error_public_text(err: object) -> str:
    parts = [repr(err), str(err), repr(getattr(err, "__dict__", {}))]
    dump = getattr(err, "model_dump", None)
    if callable(dump):
        with contextlib.suppress(Exception):
            parts.append(repr(dump()))
    return " ".join(parts)


#: The LOCKED, immutable public shape of LifecycleEventWriteError — a single machine code, nothing else.
#: No message / cause / path / event fields (any of which could carry PII). A same-type error with an
#: EXTRA field must fail red #2 (proven), so the schema cannot silently grow a PII channel.
_ACCEPTED_ERROR_SHAPE: dict[str, object] = {"code": "lifecycle_event_write_failed"}


def _error_public_shape(err: object) -> dict[str, object]:
    dump = getattr(err, "model_dump", None)
    if callable(dump):
        return dict(dump())
    return dict(getattr(err, "__dict__", {}))


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


# 2 — write failure => typed Err + zero row + exact metric + PII-free (rendered) log ---------- #


def test_write_failure_is_typed_err_zero_row_metric_and_pii_free_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    before = _metric_value() or 0.0
    sink = _mk_sink(tmp_path / "e.db")
    real = sink._write_batch
    try:
        ev = _ev(_REASON_CANARY, object_id=_OID_CANARY, namespace=_NS_CANARY)
        sink._write_batch = _LeakyWrite()  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR):
            res = _record(sink, ev)
        if not isinstance(res, Err):
            raise DefectStillPresent(f"a failed write must return Err, got {type(res).__name__}")
        # (blocker 2) concrete error TYPE + an EXACT bounded public shape — not a bare Err[object],
        # and not a same-type error that grew an extra (PII-capable) field.
        if type(res.error).__name__ != "LifecycleEventWriteError":
            raise DefectStillPresent(
                f"error must be a concrete LifecycleEventWriteError, got {type(res.error).__name__}"
            )
        shape = _error_public_shape(res.error)
        if shape != _ACCEPTED_ERROR_SHAPE:
            raise DefectStillPresent(
                f"error public shape must be EXACTLY {_ACCEPTED_ERROR_SHAPE}, got {shape}"
            )
        # defense in depth: the fully-rendered public text still carries no PII canary
        if any(
            c in _error_public_text(res.error) for c in (_REASON_CANARY, _NS_CANARY, _OID_CANARY)
        ):
            raise DefectStillPresent(
                "error object leaked reason/namespace/object_id in public fields"
            )
        # zero rows
        if sink.read_all():
            raise DefectStillPresent("a failed write must persist ZERO rows")
        # metric: exactly one unlabeled series, +1
        lines = _rendered_metric_lines()
        if len(lines) != 1 or "{" in lines[0]:
            raise DefectStillPresent(f"metric must be exactly one UNLABELED series; got {lines}")
        if (_metric_value() or 0.0) - before != 1.0:
            raise DefectStillPresent("metric must increment by exactly +1 per failure")
        # (blocker 1) exactly ONE ERROR record; FULLY rendered (incl exc_info traceback) is PII-free
        rendered = _rendered_error_logs(caplog)
        if len(rendered) != 1:
            raise DefectStillPresent(
                f"a write failure must log exactly one ERROR; got {len(rendered)}"
            )
        if any(c in rendered[0] for c in (_REASON_CANARY, _NS_CANARY, _OID_CANARY)):
            raise DefectStillPresent(
                "ERROR log leaked reason/namespace/object_id (checked incl. exc_info traceback)"
            )
    finally:
        sink._write_batch = real  # type: ignore[method-assign]
        sink.close()


# 3 — retry the SAME event_id after a transient failure => Ok + exactly one row #


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


# 6 — sustained 1000 failures: all Err PROMPTLY (bounded subprocess), zero rows, no in-memory growth #


def test_sustained_failures_all_err_zero_rows_no_growth(tmp_path: Path) -> None:
    # The workload runs in a SUBPROCESS with a bounded wall-clock, so a `record()` that BLOCKS under
    # sustained failure (instead of promptly returning Err) fails via TimeoutExpired rather than hanging.
    db = tmp_path / "e.db"
    prog = textwrap.dedent(
        f"""
        import os
        from musubi.types.common import Err
        from musubi.lifecycle.events import LifecycleEventSink
        from musubi.types.lifecycle_event import LifecycleEvent
        s = LifecycleEventSink(db_path={str(db)!r}, flush_every_n=1_000_000, flush_every_s=3600.0)
        def boom(batch):
            raise RuntimeError("injected sustained failure")
        s._write_batch = boom
        errs = 0
        for i in range(1000):
            r = s.record(LifecycleEvent(object_id="0"*27, object_type="episodic",
                namespace="eric/claude-code/episodic", from_state="provisional",
                to_state="matured", actor="t", reason=f"x{{i}}"))
            if isinstance(r, Err):
                errs += 1
        buffered = len(getattr(s, "_buffer", []))
        rows = len(s.read_all())
        os._exit(0 if (errs == 1000 and buffered == 0 and rows == 0) else 4)
        """
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True,
            timeout=_SUSTAINED_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DefectStillPresent(
            "record() under sustained failure did not refuse promptly (blocked past the bound)"
        ) from exc
    if proc.returncode != 0:
        raise DefectStillPresent(
            f"sustained failures must all be Err with zero rows and no buffer growth "
            f"(subprocess rc={proc.returncode})"
        )


# 7 — close is idempotent; a record/close race never loses an Ok event -------- #


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

    # Exercise both legal serializations of the shared lock. If record wins,
    # Ok is returned only after the row commits. If close wins, record refuses
    # with Err. No scheduling outcome may return Ok without the durable row.
    for index in range(16):
        race_db = tmp_path / f"race-{index}.db"
        racing_sink = _mk_sink(race_db)
        rendezvous = threading.Barrier(3)
        result_box: list[object] = []

        def racing_record() -> None:
            rendezvous.wait(timeout=_JOIN_TIMEOUT)
            result_box.append(_record(racing_sink, _ev(f"race-{index}")))

        def racing_close() -> None:
            rendezvous.wait(timeout=_JOIN_TIMEOUT)
            racing_sink.close()

        record_thread = threading.Thread(target=racing_record)
        close_thread = threading.Thread(target=racing_close)
        record_thread.start()
        close_thread.start()
        rendezvous.wait(timeout=_JOIN_TIMEOUT)
        record_thread.join(timeout=_JOIN_TIMEOUT)
        close_thread.join(timeout=_JOIN_TIMEOUT)
        assert not record_thread.is_alive(), "record side of close race hung"
        assert not close_thread.is_alive(), "close side of record race hung"
        assert len(result_box) == 1

        race_result = _as_result(result_box[0])
        race_rows = _mk_sink(race_db)
        try:
            reasons = [event.reason for event in race_rows.read_all()]
        finally:
            race_rows.close()
        expected = [f"race-{index}"] if race_result.is_ok() else []
        if reasons != expected:
            raise DefectStillPresent(
                f"record/close race returned {type(race_result).__name__} but rows={reasons}"
            )


# 8 — the Result must be CONSUMED at the callsite (guard + strict red) ---------- #


def _record_callsites() -> set[str]:
    src = Path(__file__).resolve().parents[2] / "src" / "musubi"
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", r"\.record(", str(src)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return {Path(ln.split(":")[0]).name for ln in out.splitlines() if "sink.record(" in ln}


def test_record_callsite_inventory_is_exactly_reviewed() -> None:
    """Guard (green now + post-fix): the set of `sink.record(` callsites must be EXACTLY the reviewed
    set — equality, not subset — so a NEW callsite fails this guard and forces Result-handling review.
    C6b removed the legacy callsite: the coordinator now persists the event atomically with FINAL."""
    reviewed: set[str] = set()
    seen = _record_callsites()
    assert seen == reviewed, (
        f"record() callsite inventory changed: seen={seen} reviewed={reviewed} — "
        "each callsite must handle Result[None, LifecycleEventWriteError] (see the C6 slice)"
    )


def test_record_result_is_consumed_not_bare_expression() -> None:
    """Strict red: AST-parse transitions.py and reject a bare `sink.record(...)` expression. The Result
    must be consumed (assigned / returned / propagated), never discarded as an expression statement."""
    trans = Path(__file__).resolve().parents[2] / "src" / "musubi" / "lifecycle" / "transitions.py"
    tree = ast.parse(trans.read_text())
    bare: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Attribute) and fn.attr == "record":
                bare.append(node.lineno)
    if bare:
        raise DefectStillPresent(
            f"sink.record() Result ignored as a bare expression at transitions.py:{bare}"
        )


class _MockFailingSink:
    """Return a refused write so the sole production caller must propagate it."""

    def record(self, event: LifecycleEvent) -> object:
        del event
        return Err(error=object())


def test_transition_ignores_retired_legacy_sink_boundary(tmp_path: Path) -> None:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="musubi_episodic",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    object_id = generate_ksuid()
    client.upsert(
        collection_name="musubi_episodic",
        points=[
            PointStruct(
                id="11111111-1111-1111-1111-111111111111",
                vector=[0.1, 0.2],
                payload={
                    "object_id": object_id,
                    "namespace": "test/presence/episodic",
                    "state": "provisional",
                    "version": 1,
                },
            )
        ],
    )

    result = transition(
        client,
        coordinator=LifecycleTransitionCoordinator(
            client=client, db_path=tmp_path / "lifecycle.db"
        ),
        object_id=object_id,
        target_state="matured",
        actor="test",
        reason="test lifecycle event refusal",
        sink=cast(LifecycleEventSink, _MockFailingSink()),
        expected_version=1,
    )

    points, _ = client.scroll(
        collection_name="musubi_episodic",
        with_payload=True,
        limit=1,
    )
    assert points[0].payload is not None
    assert points[0].payload["state"] == "matured"

    assert isinstance(result, Ok)
