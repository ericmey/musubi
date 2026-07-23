"""S3 ``_mark_applied`` idempotent-replay hardening (LifecycleJobCrashed repair).

The recurring ``LifecycleJobCrashed`` production alert was ONE defect: a second
apply pass calls ``_mark_applied`` for an operation whose outbox row was already
advanced to APPLIED/FINAL by an earlier pass (or by the winner of a two-connection
race). The guarded ``PENDING->APPLIED`` update then matched zero rows and raised —
crashing the lifecycle job even though the side effect was already applied and no
row was stranded.

These proofs pin the corrected contract directly at ``_mark_applied``:

  * ordinary PENDING -> APPLIED success moves exactly one row and writes one marker;
  * a matching APPLIED/FINAL replay returns idempotently with ZERO mutation;
  * a missing row, a mismatched identity/target, and a real lease-owner mismatch
    each fail loudly;
  * a deterministic two-connection race yields one transition, one marker, both
    callers normal, and zero crashed jobs.
"""

from __future__ import annotations

import sqlite3
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap

_OID = "obj-1"
_TS = "matured"
_COLL = "musubi_episodic"


@pytest.fixture
def coord(tmp_path: Path) -> Iterator[LifecycleTransitionCoordinator]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    c = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "lc.db")
    try:
        yield c
    finally:
        client.close()


def _seed_row(
    db: Path,
    opk: str,
    state: str,
    *,
    object_id: str = _OID,
    target_state: str = _TS,
    lease_owner: str | None = None,
) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO lifecycle_outbox "
            "(operation_key, object_id, collection, target_state, state, lease_owner) "
            "VALUES (?,?,?,?,?,?)",
            (opk, object_id, _COLL, target_state, state, lease_owner),
        )
        con.commit()
    finally:
        con.close()


def _seed_marker(db: Path, opk: str, *, object_id: str = _OID, target_state: str = _TS) -> None:
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO lifecycle_apply_markers (operation_key, object_id, target_state) "
            "VALUES (?,?,?)",
            (opk, object_id, target_state),
        )
        con.commit()
    finally:
        con.close()


def _state(db: Path, opk: str) -> str | None:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT state FROM lifecycle_outbox WHERE operation_key=?", (opk,)
        ).fetchone()
        return None if row is None else row[0]
    finally:
        con.close()


def _full_row(db: Path, opk: str) -> tuple[object, ...] | None:
    """The ENTIRE outbox row (all columns: state, attempts, lease_owner, lease_expires_epoch,
    terminal_epoch, next_attempt_epoch, ...) — a full zero-mutation fingerprint, not just state."""
    con = sqlite3.connect(str(db))
    try:
        row: tuple[object, ...] | None = con.execute(
            "SELECT * FROM lifecycle_outbox WHERE operation_key=?", (opk,)
        ).fetchone()
        return row
    finally:
        con.close()


def _marker_count(db: Path, opk: str) -> int:
    con = sqlite3.connect(str(db))
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM lifecycle_apply_markers WHERE operation_key=?", (opk,)
            ).fetchone()[0]
        )
    finally:
        con.close()


# -- ordinary success ------------------------------------------------------------ #


def test_pending_moves_to_applied_and_writes_one_marker(
    coord: LifecycleTransitionCoordinator,
) -> None:
    db = coord._db
    _seed_row(db, "op", "PENDING")
    coord._mark_applied("op", _OID, _TS)
    assert _state(db, "op") == "APPLIED"
    assert _marker_count(db, "op") == 1


# -- idempotent replay (the defect): APPLIED / FINAL return with zero mutation ---- #


@pytest.mark.parametrize("terminal", ["APPLIED", "FINAL"])
def test_matching_replay_is_idempotent_no_mutation(
    coord: LifecycleTransitionCoordinator, terminal: str
) -> None:
    db = coord._db
    _seed_row(db, "op", terminal)  # already advanced by an earlier pass
    _seed_marker(db, "op")  # its marker ALREADY existed (normal prior apply)
    before = _full_row(db, "op")
    # replay must NOT raise ...
    coord._mark_applied("op", _OID, _TS)
    # ... and must mutate NOTHING: the FULL outbox row (state, attempts, lease_owner, lease expiry,
    # terminal/next-attempt fields) is byte-identical, and still exactly one marker.
    assert _full_row(db, "op") == before
    assert _marker_count(db, "op") == 1


# -- fail-loud discriminators ---------------------------------------------------- #


def test_missing_row_fails_loud(coord: LifecycleTransitionCoordinator) -> None:
    with pytest.raises(RuntimeError, match="no lifecycle_outbox row"):
        coord._mark_applied("nope", _OID, _TS)


def test_identity_mismatch_fails_loud_and_rolls_back_marker(
    coord: LifecycleTransitionCoordinator,
) -> None:
    db = coord._db
    # row exists (already applied) but for a DIFFERENT object identity; no marker yet.
    _seed_row(db, "op", "APPLIED", object_id="other-obj")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        coord._mark_applied("op", _OID, _TS)
    # the marker insert attempted this txn must have been rolled back.
    assert _marker_count(db, "op") == 0
    assert _state(db, "op") == "APPLIED"


def test_lease_owner_mismatch_fails_loud(coord: LifecycleTransitionCoordinator) -> None:
    db = coord._db
    _seed_row(db, "op", "PENDING", lease_owner="ownerA")
    with pytest.raises(RuntimeError, match="lease mismatch"):
        coord._mark_applied("op", _OID, _TS, owner="ownerB")
    # not stolen: still PENDING under ownerA, no marker committed.
    assert _state(db, "op") == "PENDING"
    assert _marker_count(db, "op") == 0


def test_target_state_mismatch_fails_loud_and_no_mutation(
    coord: LifecycleTransitionCoordinator,
) -> None:
    # SAME object_id, DIFFERENT target_state — an identity mismatch independent of object_id (F2).
    db = coord._db
    _seed_row(db, "op", "APPLIED", target_state="archived")  # call passes _TS="matured"
    before = _full_row(db, "op")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        coord._mark_applied("op", _OID, _TS)
    assert _marker_count(db, "op") == 0
    assert _full_row(db, "op") == before


def test_terminal_row_without_preexisting_marker_is_corruption(
    coord: LifecycleTransitionCoordinator,
) -> None:
    # A terminal row whose S3 apply marker NEVER existed is corruption, NOT a benign replay (F1):
    # marker+APPLIED are atomic, so an APPLIED/FINAL row with no marker must fail loud, not converge.
    db = coord._db
    _seed_row(db, "op", "APPLIED")  # matching identity, but NO marker seeded
    before = _full_row(db, "op")
    with pytest.raises(RuntimeError, match="corruption"):
        coord._mark_applied("op", _OID, _TS)
    # the marker we inserted this txn is rolled back; the row is untouched.
    assert _marker_count(db, "op") == 0
    assert _full_row(db, "op") == before


def test_abandoned_state_fails_loud(coord: LifecycleTransitionCoordinator) -> None:
    db = coord._db
    _seed_row(db, "op", "ABANDONED")
    with pytest.raises(RuntimeError, match="unexpected state"):
        coord._mark_applied("op", _OID, _TS)


# -- deterministic two-connection race ------------------------------------------- #


def test_two_connection_race_one_transition_one_marker_both_normal(
    coord: LifecycleTransitionCoordinator,
) -> None:
    db = coord._db
    _seed_row(db, "op", "PENDING")
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _call() -> None:
        try:
            barrier.wait(timeout=5)
            coord._mark_applied("op", _OID, _TS)
        except BaseException as exc:  # catch ANY crash — a crashed lifecycle job IS the defect
            errors.append(exc)

    t1 = threading.Thread(target=_call)
    t2 = threading.Thread(target=_call)
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    assert not errors, f"a caller crashed: {errors!r}"  # zero crashed jobs
    assert _state(db, "op") == "APPLIED"  # exactly one transition
    assert _marker_count(db, "op") == 1  # exactly one marker
