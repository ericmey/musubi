"""S2 admission-layer proofs for the durable-intent coordinator (client-free).

These exercise ONLY the admission half of ``transition()`` — the paths that resolve at the
durable boundary (operation_key idempotency, the cap gate, single-active-intent, durable-begin
failures, and the before/after ``_pending_commit`` crash seam) BEFORE any Qdrant client is
needed. The full apply + finalize contract (S3) is proven in ``test_s3_coordinator_apply.py``.

Admission failures/faults are driven with ``client=None`` (they return or propagate before the
pre-apply read), and durable PENDING rows are seeded via ``_write_pending`` directly, so no
Qdrant is ever constructed here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from musubi.lifecycle import store
from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionIntent,
    _CapExceeded,
)
from musubi.types.common import Err


def _fault_at(target: str) -> Callable[[str], None]:
    """A _checkpoint seam that raises a real sqlite error at exactly one named boundary."""

    def _cp(name: str) -> None:
        if name == target:
            raise sqlite3.OperationalError(f"injected fault at {target}")

    return _cp


def _intent(object_id: str, operation_key: str | None = None) -> TransitionIntent:
    return TransitionIntent(
        collection="musubi_episodic",
        object_id=object_id,
        namespace="tenant/presence",
        expected_version=1,
        target_state="matured",
        actor="t",
        reason="r",
        operation_key=operation_key,
    )


def _coord(db: Path, *, pending_cap: int = 10_000) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=None, db_path=db, pending_cap=pending_cap)


def _seed_pending(coord: LifecycleTransitionCoordinator, intent: TransitionIntent, opk: str) -> str:
    """Seed a durable PENDING row via the admission primitive directly (no Qdrant)."""
    coord._write_pending(intent, opk, "ev-" + opk)
    return opk


def _nonterminal_count(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM lifecycle_outbox WHERE state IN ('PENDING','APPLIED')"
            ).fetchone()[0]
        )
    finally:
        con.close()


def _rows_for_object(db: Path, object_id: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(str(db))
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


def test_cap_rejects_at_cap(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    coord = _coord(db, pending_cap=3)
    for i in range(3):  # fill to exactly the cap via admission-direct writes (distinct objects)
        _seed_pending(coord, _intent(f"o{i}"), f"k{i}")
    # a NEW admission at the cap is cap_exceeded and writes no row — resolved before any client.
    res = coord.transition(_intent("over", operation_key="k-over"))
    assert isinstance(res, Err)
    assert res.error.code == "cap_exceeded"
    assert _nonterminal_count(db) == 3
    assert _rows_for_object(db, "over") == []
    # and the raw admission primitive raises _CapExceeded (writing no row) at the boundary.
    with pytest.raises(_CapExceeded):
        coord._write_pending(_intent("over2"), "k-over2", "ev")


def test_single_active_same_object_rejects(tmp_path: Path) -> None:
    db = tmp_path / "lc.db"
    coord = _coord(db, pending_cap=100)
    _seed_pending(coord, _intent("o1", operation_key="k1"), "k1")
    # a SECOND active intent for the same object (distinct key) -> active_intent_exists (the
    # ux_active_intent partial-unique index), exactly one active intent survives.
    second = coord.transition(_intent("o1", operation_key="k2"))
    assert isinstance(second, Err)
    assert second.error.code == "active_intent_exists"
    assert len(_rows_for_object(db, "o1")) == 1


def test_operation_key_reuse_is_conflict_or_durable_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "lc.db"
    coord = _coord(db, pending_cap=100)
    # (a) reusing an operation_key for a DIFFERENT intent resolves at the replay step (idempotency
    # BEFORE the cap / any mutation) as operation_key_conflict — no new row.
    _seed_pending(coord, _intent("oa", operation_key="dup"), "dup")
    conflict = coord.transition(
        _intent("ob", operation_key="dup")
    )  # distinct object -> distinct digest
    assert isinstance(conflict, Err)
    assert conflict.error.code == "operation_key_conflict"
    assert _rows_for_object(db, "ob") == []

    # (b) WARN-2 relocated: a durable-path store.connect that cannot establish the WAL policy raises
    # store.LifecycleStoreError (a RuntimeError, NOT sqlite3.Error) -> durable_begin_failed, no row.
    def _boom(*_a: object, **_k: object) -> object:
        raise store.LifecycleStoreError("injected WAL-policy failure at the durable path")

    monkeypatch.setattr(store, "connect", _boom)
    res = coord.transition(_intent("oc", operation_key="k-oc"))
    monkeypatch.undo()
    assert isinstance(res, Err)
    assert res.error.code == "durable_begin_failed"
    assert _rows_for_object(db, "oc") == []


def test_admission_crash_seam_faults(tmp_path: Path) -> None:
    # WARN-1 relocated. A fault at before_pending_commit is a durable-begin failure (bounded Err,
    # no row). A fault at after_pending_commit is OUTSIDE the durable-begin catch -> it PROPAGATES
    # on an already-committed PENDING row and is NEVER mapped to durable_begin_failed. Both resolve
    # before the pre-apply read, so client=None is never reached.
    before_db = tmp_path / "before.db"
    before = _coord(before_db)
    before._checkpoint = _fault_at("before_pending_commit")
    bres = before.transition(_intent("pre"))
    assert isinstance(bres, Err)
    assert bres.error.code == "durable_begin_failed"
    assert _rows_for_object(before_db, "pre") == []

    after_db = tmp_path / "after.db"
    after = _coord(after_db)
    after._checkpoint = _fault_at("after_pending_commit")
    with pytest.raises(sqlite3.OperationalError):
        after.transition(_intent("post"))
    committed = _rows_for_object(after_db, "post")
    assert len(committed) == 1 and committed[0][1] == "PENDING"
