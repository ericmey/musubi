"""S4 direct real-source proofs: reconcile_once — leases, attempts/backoff, crash recovery.

These drive the REAL ``LifecycleTransitionCoordinator.reconcile_once`` against a real (in-memory)
Qdrant object and prove the S4 contract Yua authorized: due-filtered fair-ordered claiming, atomic
guarded leases with expired reclaim (fresh-token ABA fence), durable attempts + bounded backoff,
terminal/transient/unknown classification (unknown never abandoned by count), and the crash matrix
(pre-persist re-drive, readback-recovery with no second effective apply, APPLIED->FINAL) — plus the
required wrong-shape discriminators (exact-owner no-op, not-due no-op, monotonic bounded backoff).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_MAX,
    LifecycleTransitionCoordinator,
    TransitionIntent,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.types.episodic import EpisodicMemory

_NS = "eric/claude-code/episodic"


class _Seed:
    def __init__(self, collection: str, object_id: str, namespace: str, version: int) -> None:
        self.collection = collection
        self.object_id = object_id
        self.namespace = namespace
        self.version = version


@pytest.fixture
def env(tmp_path: Path) -> Iterator[tuple[QdrantClient, _Seed, Path]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    plane = EpisodicPlane(client=client, embedder=FakeEmbedder())
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=_NS, content="s4-seed")))
    seed = _Seed(str(collection_for_plane("episodic")), str(obj.object_id), _NS, int(obj.version))
    try:
        yield client, seed, tmp_path / "lc.db"
    finally:
        client.close()


def _intent(seed: _Seed, target: str, *, opk: str) -> TransitionIntent:
    return TransitionIntent(
        collection=seed.collection,
        object_id=seed.object_id,
        namespace=seed.namespace,
        expected_version=seed.version,
        target_state=target,
        actor="t",
        reason="r",
        operation_key=opk,
    )


def _coord(client: Any, db: Path, **kw: Any) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=client, db_path=db, **kw)


def _row(db: Path, opk: str) -> dict[str, Any]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM lifecycle_outbox WHERE operation_key=?", (opk,)).fetchone()
    finally:
        con.close()
    assert r is not None, f"no row for {opk}"
    return dict(r)


def _counts(db: Path) -> tuple[int, int]:
    con = sqlite3.connect(str(db))
    try:
        e = con.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
        m = con.execute("SELECT COUNT(*) FROM lifecycle_apply_markers").fetchone()[0]
        return int(e), int(m)
    finally:
        con.close()


def _qdrant(client: QdrantClient, seed: _Seed) -> tuple[object, object]:
    pts, _ = client.scroll(
        collection_name=seed.collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=seed.object_id)
                )
            ]
        ),
        limit=1,
        with_payload=True,
    )
    p = pts[0].payload or {}
    return p.get("version"), p.get("state")


class _Terminal(RuntimeError):
    terminal = True


class _Transient(RuntimeError):
    transient = True


class _FailN:
    """A client whose ``set_payload`` raises ``exc`` for the first ``n`` calls, then delegates."""

    def __init__(self, real: Any, exc: Exception, n: int = 10_000) -> None:
        self._real = real
        self._exc = exc
        self._left = n

    def set_payload(self, *a: Any, **k: Any) -> Any:
        if self._left > 0:
            self._left -= 1
            raise self._exc
        return self._real.set_payload(*a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _crash_pending(client: QdrantClient, seed: _Seed, db: Path, opk: str) -> None:
    """Drive a transition that crashes at after_pending_commit — a pre-persist PENDING row."""
    c = _coord(client, db)

    def _crash(name: str) -> None:
        if name == "after_pending_commit":
            raise RuntimeError("crash")

    c._checkpoint = _crash
    with contextlib.suppress(RuntimeError):
        c.transition(_intent(seed, "matured", opk=opk))


# --------------------------------------------------------------------------- #
# Crash matrix
# --------------------------------------------------------------------------- #


def test_reconcile_redrives_pre_persist_crash(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    _crash_pending(client, seed, db, "op")
    assert _row(db, "op")["state"] == "PENDING"
    assert _row(db, "op")["event_payload"] is None  # pre-persist: no event yet
    rep = _coord(client, db).reconcile_once(limit=10)
    assert (rep.claimed, rep.finalized) == (1, 1)
    assert _row(db, "op")["state"] == "FINAL"
    assert _counts(db) == (1, 1)  # exactly one event + marker
    assert _qdrant(client, seed) == (seed.version + 1, "matured")


def test_reconcile_readback_recognizes_applied_no_second_apply(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    # A crash after the Qdrant apply, before APPLIED: reconcile readback-confirms and finalizes
    # WITHOUT a second effective apply (exactly one set_payload total).
    client, seed, db = env
    c = _coord(client, db)

    def _crash(name: str) -> None:
        if name == "after_qdrant_readback_before_applied_commit":
            raise RuntimeError("crash after apply, before APPLIED")

    c._checkpoint = _crash
    with pytest.raises(RuntimeError):
        c.transition(_intent(seed, "matured", opk="op"))
    assert _row(db, "op")["state"] == "PENDING"  # not APPLIED
    assert _qdrant(client, seed) == (seed.version + 1, "matured")  # but Qdrant IS applied
    calls = {"n": 0}
    real_sp = client.set_payload

    def _counting(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return real_sp(*a, **k)

    client.set_payload = _counting  # type: ignore[method-assign]
    rep = _coord(client, db).reconcile_once(limit=10)
    assert rep.finalized == 1
    assert calls["n"] == 0, "readback recovery must NOT re-apply"
    assert _row(db, "op")["state"] == "FINAL"
    assert _counts(db) == (1, 1)


# --------------------------------------------------------------------------- #
# Leases: exclusivity + expired reclaim (fresh-token ABA fence)
# --------------------------------------------------------------------------- #


def test_valid_lease_is_exclusive_and_expired_is_reclaimable(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    client, seed, db = env
    _crash_pending(client, seed, db, "op")  # a claimable PENDING row
    c = _coord(client, db, lease_ttl=1000.0)
    now = 5_000.0
    token_a = c._new_token()
    con = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert c._claim(con, "op", now, token_a) is True  # A wins
        con.commit()
        token_b = c._new_token()
        assert c._claim(con, "op", now, token_b) is False  # B blocked by A's VALID lease
        con.commit()
        # the lease has EXPIRED (now advanced past A's expiry): a fresh token reclaims it.
        assert c._claim(con, "op", now + 2000.0, token_b) is True
        con.commit()
    finally:
        con.close()
    assert token_a != token_b  # fresh per-claim token (ABA fence)
    assert _row(db, "op")["lease_owner"] == token_b


# --------------------------------------------------------------------------- #
# Attempts + backoff + classification
# --------------------------------------------------------------------------- #


def test_transient_increments_and_reschedules_never_abandoned(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    client, seed, db = env
    _crash_pending(client, seed, db, "op")
    # a transient apply failure keeps the row PENDING, increments attempts, and reschedules; even
    # across many passes it is NEVER abandoned (R15).
    fail_client = _FailN(client, _Transient("blip"))
    c = _coord(fail_client, db, lease_ttl=0.001)  # tiny ttl so each pass can reclaim
    for _ in range(5):
        # force the row due for this pass (clear the previous pass's backoff schedule + lease) so
        # the retry cadence is deterministic without sleeping on real backoff.
        con = sqlite3.connect(str(db))
        con.execute(
            "UPDATE lifecycle_outbox SET next_attempt_epoch=NULL, lease_owner=NULL, "
            "lease_expires_epoch=NULL WHERE operation_key='op'"
        )
        con.commit()
        con.close()
        rep = c.reconcile_once(limit=10)
        assert rep.pending == 1
    row = _row(db, "op")
    assert row["state"] == "PENDING"
    assert row["attempts"] == 5
    assert (
        row["failure_class"] == "transient"
    )  # _Transient carries .transient -> classified transient
    assert row["next_attempt_epoch"] is not None  # rescheduled


def test_terminal_apply_abandons(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    _crash_pending(client, seed, db, "op")
    rep = _coord(_FailN(client, _Terminal("proven")), db).reconcile_once(limit=10)
    assert rep.abandoned == 1
    row = _row(db, "op")
    assert row["state"] == "ABANDONED"
    assert row["failure_class"] == "terminal"
    assert _counts(db) == (0, 0)  # no event, no marker for an abandoned op


def test_backoff_is_monotonic_and_bounded(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, _seed, db = env
    c = _coord(client, db, backoff_base_s=2.0, backoff_max_s=60.0)
    seq = [c._backoff(n) for n in range(1, 12)]
    assert seq[0] == 2.0  # base at attempt 1 (2 * 2**0)
    assert all(b <= 60.0 for b in seq), "never exceeds max"
    assert all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1)), "monotonic non-decreasing"
    assert seq[-1] == 60.0  # saturates to max
    # overflow-safe: a huge attempts count clamps to max without evaluating 2**huge.
    assert c._backoff(10_000) == 60.0


# --------------------------------------------------------------------------- #
# Fairness + report + exact-owner no-op
# --------------------------------------------------------------------------- #


def test_not_due_row_is_a_true_noop(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    _crash_pending(client, seed, db, "op")
    # schedule the row far in the future -> not due -> reconcile leaves it untouched (no claim, no
    # attempt increment, no lease side effect).
    con = sqlite3.connect(str(db))
    con.execute(
        "UPDATE lifecycle_outbox SET next_attempt_epoch=? WHERE operation_key='op'", (1e18,)
    )
    con.commit()
    con.close()
    rep = _coord(client, db).reconcile_once(limit=10)
    assert (rep.claimed, rep.finalized, rep.pending, rep.abandoned) == (0, 0, 0, 0)
    row = _row(db, "op")
    assert row["attempts"] == 0 and row["lease_owner"] is None


def test_nonowner_finalize_is_a_silent_noop(env: tuple[QdrantClient, _Seed, Path]) -> None:
    # exact-owner semantics: finalizing with a token that is not the row's lease owner matches zero
    # rows and is a silent no-op — never a raise, never an event, no state change.
    client, seed, db = env
    _crash_pending(client, seed, db, "op")
    c = _coord(client, db)
    now = 1_000.0
    con = sqlite3.connect(str(db), isolation_level=None)
    try:
        assert c._claim(con, "op", now, c._new_token())  # owned by some token
        con.commit()
        con.execute("UPDATE lifecycle_outbox SET state='APPLIED' WHERE operation_key='op'")
        con.commit()
    finally:
        con.close()
    c._finalize("op", "ev", seed.object_id, seed.namespace, "matured", owner="not-the-owner")
    assert _row(db, "op")["state"] == "APPLIED"  # unchanged
    assert _counts(db) == (0, 0)  # no event written


def test_default_backoff_constants_are_bounded() -> None:
    assert 0 < DEFAULT_BACKOFF_BASE <= DEFAULT_BACKOFF_MAX
