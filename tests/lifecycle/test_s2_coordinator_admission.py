"""S2 DIRECT production-source proofs for the durable-intent admission coordinator.

These are the LOAD-BEARING behavioral evidence that `musubi.lifecycle.coordinator`
(the real source, never the test-local `_RefCoordinator`) implements atomic admission:
durable PENDING, one active intent per (collection, object_id), and a hard pending cap.
The two cross-process races spawn subprocesses that FRESH-IMPORT the production
coordinator in each child and rendezvous at `before_pending_commit` so both children
reach the admission boundary before either inserts. S2 never applies to Qdrant, so
BOTH winner and loser must leave zero Qdrant-touch markers.

The frozen C6b reds test_r11 / test_r14_two_process_admission_race flip mechanically
(their `_api()` import-gate XPASSes once this module imports) but are NOT the behavioral
proof — these tests are. (See slice-c6b-phase1-source-impl Test Contract.)
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from musubi.lifecycle import store
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionIntent
from musubi.types.common import Err, Ok


def _fault_at(target: str) -> Callable[[str], None]:
    """A _checkpoint seam that raises a real sqlite error at exactly one named boundary."""

    def _cp(name: str) -> None:
        if name == target:
            raise sqlite3.OperationalError(f"injected fault at {target}")

    return _cp


_WIN = 21
_CONFLICT = 22
_CAP = 23
_N_PROCS = 2


def _intent(object_id: str, operation_key: str | None = None) -> TransitionIntent:
    return TransitionIntent(
        collection="episodic",
        object_id=object_id,
        namespace="tenant/presence",
        expected_version=1,
        target_state="matured",
        actor="t",
        reason="r",
        operation_key=operation_key,
    )


def _nonterminal_count(db_path: Path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
            ).fetchone()[0]
        )
    finally:
        con.close()


def _rows_for_object(db_path: Path, object_id: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(str(db_path))
    try:
        return [
            (str(k), str(s))
            for k, s in con.execute(
                "SELECT operation_key, state FROM lifecycle_outbox "
                "WHERE object_id = ? AND state IN ('PENDING','APPLIED')",
                (object_id,),
            ).fetchall()
        ]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Single-process direct proofs
# ---------------------------------------------------------------------------


def test_admission_writes_pending_and_returns_ok_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coord = LifecycleTransitionCoordinator(client=None, db_path=tmp_path / "lc.db")
    res = coord.transition(_intent("o1"))
    assert isinstance(res, Ok)
    assert res.value.kind == "pending"
    rows = _rows_for_object(tmp_path / "lc.db", "o1")
    assert len(rows) == 1
    assert rows[0][0] == res.value.operation_key
    assert rows[0][1] == "PENDING"

    # WARN-2 fold — a per-operation store.connect that cannot establish the WAL policy
    # raises store.LifecycleStoreError, a RuntimeError (NOT sqlite3.Error). It must still
    # be classified durable_begin_failed with no row. Patch AFTER construction so only the
    # per-call connect path fails (constructor schema-open stays fail-fast, per spec).
    def _boom(*_a: object, **_k: object) -> object:
        raise store.LifecycleStoreError("injected WAL-policy failure at per-op connect")

    monkeypatch.setattr(store, "connect", _boom)
    conn_fail = coord.transition(_intent("oc", operation_key="k-oc"))
    monkeypatch.undo()
    assert isinstance(conn_fail, Err)
    assert conn_fail.error.code == "durable_begin_failed"
    assert _rows_for_object(tmp_path / "lc.db", "oc") == []  # no row on a connect failure

    # WARN-1 fold — post-commit-checkpoint fault must NOT false-fail a committed row.
    # after_pending_commit fires OUTSIDE the durable-begin catch: a fault there must
    # PROPAGATE (crash/race seam) on an already-committed row, never be swallowed into
    # Err(durable_begin_failed) (which would drive a spurious retry of a durable write).
    after_db = tmp_path / "after.db"
    after_coord = LifecycleTransitionCoordinator(client=None, db_path=after_db)
    after_coord._checkpoint = _fault_at("after_pending_commit")
    with pytest.raises(sqlite3.OperationalError):
        after_coord.transition(_intent("post"))
    committed = _rows_for_object(after_db, "post")
    assert len(committed) == 1 and committed[0][1] == "PENDING"  # row IS durably committed

    # A fault at before_pending_commit, by contrast, is a genuine begin failure — it is
    # inside the catch → bounded Err(durable_begin_failed), no row.
    before_db = tmp_path / "before.db"
    before_coord = LifecycleTransitionCoordinator(client=None, db_path=before_db)
    before_coord._checkpoint = _fault_at("before_pending_commit")
    bres = before_coord.transition(_intent("pre"))
    assert isinstance(bres, Err)
    assert bres.error.code == "durable_begin_failed"
    assert _rows_for_object(before_db, "pre") == []  # no row on a real begin failure


def test_cap_rejects_at_cap(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    coord = LifecycleTransitionCoordinator(client=None, db_path=db, pending_cap=3)
    for i in range(3):  # fill to exactly the cap with distinct objects
        assert isinstance(coord.transition(_intent(f"o{i}")), Ok)
    res = coord.transition(_intent("over"))
    assert isinstance(res, Err)
    assert res.error.code == "cap_exceeded"
    assert _nonterminal_count(db) == 3  # no new row admitted at the cap
    assert _rows_for_object(db, "over") == []


def test_single_active_same_object_rejects(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    coord = LifecycleTransitionCoordinator(client=None, db_path=db, pending_cap=100)
    first = coord.transition(_intent("o1", operation_key="k1"))
    assert isinstance(first, Ok)
    second = coord.transition(_intent("o1", operation_key="k2"))  # same object, different key
    assert isinstance(second, Err)
    assert second.error.code == "active_intent_exists"
    assert len(_rows_for_object(db, "o1")) == 1  # exactly one active intent survives

    # Correction-3 fold — classification boundary of active_intent_exists. ONLY the
    # ux_active_intent (collection, object_id) partial-unique violation is
    # active_intent_exists. A duplicate operation_key (PRIMARY KEY) on a DIFFERENT object
    # is a generic durable_begin_failed — the two must never be conflated.
    assert isinstance(coord.transition(_intent("oa", operation_key="dup")), Ok)
    dup = coord.transition(_intent("ob", operation_key="dup"))  # dup key, distinct object
    assert isinstance(dup, Err)
    assert dup.error.code == "durable_begin_failed"
    assert dup.error.code != "active_intent_exists"


# ---------------------------------------------------------------------------
# Two-process direct races — production coordinator fresh-imported in each child
# ---------------------------------------------------------------------------


def _child_source(
    *, db: Path, barrier: Path, object_id: str, cap: int, operation_key: str | None = None
) -> str:
    return textwrap.dedent(f"""
        import os, sys, time
        from pathlib import Path
        # FRESH import of the REAL production coordinator in this child process.
        from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionIntent

        _bd = {str(barrier)!r}
        _db = {str(db)!r}
        _oid = {object_id!r}
        _opk = {operation_key!r}
        _pid = str(os.getpid())

        class _MarkerClient:
            # S2 admission must NEVER call Qdrant. If it ever did, this leaves a marker.
            def set_payload(self, *a, **k):
                open(os.path.join(_bd, "touched." + _pid), "w").close()
            def __getattr__(self, name):
                def _rec(*a, **k):
                    open(os.path.join(_bd, "touched." + _pid), "w").close()
                return _rec

        def _cp(name):
            if name == "before_pending_commit":
                # Signal that THIS child reached the admission boundary, then wait until
                # BOTH children have — so neither inserts before both are here.
                open(os.path.join(_bd, "reached." + _pid), "w").close()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if len([f for f in os.listdir(_bd) if f.startswith("reached.")]) >= {_N_PROCS}:
                        return
                    time.sleep(0.01)
                sys.exit(90)  # barrier timeout — unexpected
            if name == "after_pending_commit":
                os._exit({_WIN})  # winner committed its PENDING row

        c = LifecycleTransitionCoordinator(client=_MarkerClient(), db_path=Path(_db), pending_cap={cap})
        c._checkpoint = _cp
        res = c.transition(TransitionIntent(
            collection="episodic", object_id=_oid, namespace="tenant/presence",
            expected_version=1, target_state="matured", actor="t", reason="r",
            operation_key=_opk,
        ))
        code = getattr(getattr(res, "error", None), "code", None)
        if code == "active_intent_exists":
            os._exit({_CONFLICT})
        if code == "cap_exceeded":
            os._exit({_CAP})
        os._exit(99)  # unexpected outcome
    """)


def _run_race(db: Path, barrier: Path, sources: list[str]) -> tuple[list[int], list[str]]:
    barrier.mkdir(parents=True, exist_ok=True)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", src], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for src in sources
    ]
    codes: list[int] = []
    stderrs: list[str] = []
    for p in procs:
        try:
            _out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            _out, err = p.communicate()
            codes.append(-1)
            stderrs.append("TIMEOUT: " + err.decode(errors="ignore"))
            continue
        codes.append(int(p.returncode))
        stderrs.append(err.decode(errors="ignore"))
    # Correction (1): prove BOTH children actually reached before_pending_commit.
    reached = [f for f in os.listdir(barrier) if f.startswith("reached.")]
    assert len(reached) == _N_PROCS, (
        f"both children must reach before_pending_commit; saw {reached}\nstderr: {stderrs}"
    )
    return codes, stderrs


def _assert_no_qdrant_touch(barrier: Path, stderrs: list[str]) -> None:
    # Correction (2): ZERO Qdrant touch for BOTH winner and loser (S2 never applies).
    touched = [f for f in os.listdir(barrier) if f.startswith("touched.")]
    assert touched == [], f"S2 admission must not touch Qdrant (any child); saw {touched}"


def test_two_process_single_active_admits_one_rejects_conflict(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    barrier = tmp_path / "barrier"
    LifecycleTransitionCoordinator(client=None, db_path=db)  # create shared schema
    # DISTINCT operation keys, SAME object → only the ux_active_intent (collection,
    # object_id) partial-unique index — not the operation_key PK — can reject the loser.
    src_a = _child_source(
        db=db, barrier=barrier, object_id="shared-obj", cap=10_000, operation_key="op-a"
    )
    src_b = _child_source(
        db=db, barrier=barrier, object_id="shared-obj", cap=10_000, operation_key="op-b"
    )
    codes, stderrs = _run_race(db, barrier, [src_a, src_b])
    assert sorted(codes) == [_WIN, _CONFLICT], (
        f"expected one WIN one CONFLICT; got {codes}\n{stderrs}"
    )
    assert len(_rows_for_object(db, "shared-obj")) == 1, "exactly one active intent may survive"
    _assert_no_qdrant_touch(barrier, stderrs)


def test_two_process_cap_admission_holds_cap(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    barrier = tmp_path / "barrier"
    cap = 3
    prefill = LifecycleTransitionCoordinator(client=None, db_path=db, pending_cap=cap)
    for i in range(cap - 1):  # backlog at cap-1 with distinct objects
        assert isinstance(prefill.transition(_intent(f"pre{i}")), Ok)
    src_a = _child_source(db=db, barrier=barrier, object_id="race-a", cap=cap)
    src_b = _child_source(db=db, barrier=barrier, object_id="race-b", cap=cap)
    codes, stderrs = _run_race(db, barrier, [src_a, src_b])
    assert sorted(codes) == [_WIN, _CAP], f"expected one WIN one CAP; got {codes}\n{stderrs}"
    assert _nonterminal_count(db) == cap, "backlog must settle at exactly the cap, not cap+1"
    _assert_no_qdrant_touch(barrier, stderrs)
