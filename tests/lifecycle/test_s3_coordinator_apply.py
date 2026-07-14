"""S3 direct real-source proofs: full transition() conditional apply + finalize.

These drive the REAL ``LifecycleTransitionCoordinator`` end to end against a real (in-memory)
Qdrant object and prove the S3 contract Yua authorized: conditional server-fenced apply with a
full readback, a canonical lifecycle event persisted BEFORE the mutation, atomic marker+APPLIED,
an atomic 8-column finalize, and the three-way Final/Pending/Err classification — plus the
required wrong-shape discriminators (server filter vs client read-then-write, version-only
readback, wrong namespace/object, partial patch hash, split finalize txn, duplicate/concurrent
operation_key, terminal-vs-transient, post-commit crash truth) and the collection->object_type
parity check.

The two admission/apply races use a DETERMINISTIC client whose ``set_payload`` blocks on a gate,
so the winner pauses at the mutation boundary (post-durable-admission, pre-mutation); only then is
the loser observed (rejected at admission, ZERO Qdrant), the winner released, and its single
FINAL/event/marker asserted. Scheduler speed never decides the loser outcome.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle import coordinator as coord_mod
from musubi.lifecycle.coordinator import (
    LifecycleTransitionCoordinator,
    TransitionIntent,
    _canonical_patch_sha,
    _intended_patch,
)
from musubi.planes.episodic import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.types.common import Err, Ok
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
    obj = asyncio.run(plane.create(EpisodicMemory(namespace=_NS, content="s3-seed")))
    seed = _Seed(str(collection_for_plane("episodic")), str(obj.object_id), _NS, int(obj.version))
    try:
        yield client, seed, tmp_path / "lc.db"
    finally:
        client.close()


def _intent(
    seed: _Seed,
    target: str,
    *,
    opk: str | None = None,
    expected: int | None = None,
    actor: str = "t",
    reason: str = "r",
) -> TransitionIntent:
    return TransitionIntent(
        collection=seed.collection,
        object_id=seed.object_id,
        namespace=seed.namespace,
        expected_version=seed.version if expected is None else expected,
        target_state=target,
        actor=actor,
        reason=reason,
        operation_key=opk,
    )


def _coord(client: Any, db: Path, *, pending_cap: int = 10_000) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=client, db_path=db, pending_cap=pending_cap)


def _qdrant_state(client: QdrantClient, seed: _Seed) -> tuple[object, object] | None:
    points, _ = client.scroll(
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
    if not points:
        return None
    payload = points[0].payload or {}
    return payload.get("version"), payload.get("state")


def _outbox(db: Path, opk: str) -> tuple[str, object]:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute(
            "SELECT state, event_payload FROM lifecycle_outbox WHERE operation_key=?", (opk,)
        ).fetchone()
    finally:
        con.close()
    assert row is not None, f"no outbox row for {opk}"
    return row[0], row[1]


def _counts(db: Path) -> tuple[int, int]:
    con = sqlite3.connect(str(db))
    try:
        events = con.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
        markers = con.execute("SELECT COUNT(*) FROM lifecycle_apply_markers").fetchone()[0]
        return int(events), int(markers)
    finally:
        con.close()


class _TerminalErr(RuntimeError):
    terminal = True


class _TransientErr(RuntimeError):
    transient = True


class _FailingClient:
    """Wraps a real client; ``set_payload`` raises ``exc`` (reads still pass through)."""

    def __init__(self, real: Any, exc: Exception) -> None:
        self._real = real
        self._exc = exc

    def set_payload(self, *a: object, **k: object) -> None:
        raise self._exc

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _BarrierClient:
    """Deterministic race client: ``set_payload`` signals ``reached`` then blocks on ``gate`` before
    performing the real mutation — so the winner pauses at the mutation boundary until released."""

    def __init__(self, real: Any, gate: threading.Event, reached: threading.Event) -> None:
        self._real = real
        self._gate = gate
        self._reached = reached

    def set_payload(self, *a: Any, **k: Any) -> Any:
        self._reached.set()
        if not self._gate.wait(timeout=20):
            raise RuntimeError("barrier gate never released")
        return self._real.set_payload(*a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


# --------------------------------------------------------------------------- #
# Full-contract happy path + invariants
# --------------------------------------------------------------------------- #


def test_happy_full_transition_reaches_final(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    res = _coord(client, db).transition(_intent(seed, "matured", opk="op"))
    assert isinstance(res, Ok)
    assert res.value.kind == "final"
    assert _outbox(db, "op")[0] == "FINAL"
    assert _outbox(db, "op")[1]  # a canonical event payload was persisted
    assert _counts(db) == (1, 1)  # exactly one FINAL event and one apply marker
    assert _qdrant_state(client, seed) == (seed.version + 1, "matured")


def test_event_is_persisted_before_the_qdrant_mutation(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    client, seed, db = env
    observed: dict[str, object] = {}

    class _ObservingClient:
        def set_payload(self, *a: Any, **k: Any) -> Any:
            # At the moment of the mutation, the durable event payload must ALREADY be persisted.
            observed["payload_at_mutation"] = _outbox(db, "op")[1]
            return client.set_payload(*a, **k)

        def __getattr__(self, name: str) -> Any:
            return getattr(client, name)

    res = _coord(_ObservingClient(), db).transition(_intent(seed, "matured", opk="op"))
    assert isinstance(res, Ok)
    assert observed["payload_at_mutation"], "event payload must be persisted BEFORE the mutation"


def test_persist_event_requires_exactly_one_pending_row(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    # If the PENDING row vanishes between admission and event persist, persist_event matches zero
    # rows -> a pre-mutation terminal failure; the mutation is NEVER attempted (integrity hole 1).
    client, seed, db = env
    c = _coord(client, db)

    def _delete_row_before_persist(name: str) -> None:
        if name == "after_pending_commit":
            con = sqlite3.connect(str(db))
            con.execute("DELETE FROM lifecycle_outbox WHERE operation_key='op'")
            con.commit()
            con.close()

    c._checkpoint = _delete_row_before_persist
    res = c.transition(_intent(seed, "matured", opk="op"))
    assert isinstance(res, Err)
    assert res.error.code == "terminal_apply_failure"
    assert _qdrant_state(client, seed) == (seed.version, "provisional")  # untouched


# --------------------------------------------------------------------------- #
# Deterministic admission/apply races
# --------------------------------------------------------------------------- #


def test_two_process_single_active_admits_one_rejects_conflict(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    client, seed, db = env
    gate, reached = threading.Event(), threading.Event()
    winner = _coord(_BarrierClient(client, gate, reached), db)
    loser = _coord(client, db)
    out: dict[str, Any] = {}

    def _run_winner() -> None:
        out["winner"] = winner.transition(_intent(seed, "matured", opk="winner"))

    thread = threading.Thread(target=_run_winner)
    thread.start()
    assert reached.wait(timeout=20), "winner never reached the mutation boundary"
    # Winner is paused post-admission, pre-mutation. Observe the loser now.
    state_before = _qdrant_state(client, seed)
    loser_res = loser.transition(_intent(seed, "matured", opk="loser"))
    assert isinstance(loser_res, Err)
    assert loser_res.error.code == "active_intent_exists"
    assert _qdrant_state(client, seed) == state_before, (
        "the rejected loser must make zero Qdrant writes"
    )
    gate.set()
    thread.join(timeout=20)
    assert isinstance(out["winner"], Ok) and out["winner"].value.kind == "final"
    assert _counts(db) == (1, 1)  # exactly one FINAL event + one marker (winner only)
    assert _qdrant_state(client, seed) == (seed.version + 1, "matured")


def test_two_process_cap_admission_holds_cap(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    cap = 3
    prefill = _coord(client, db, pending_cap=cap)
    for i in range(cap - 1):  # backlog at cap-1 with distinct objects (admission-direct)
        prefill._write_pending(_intent(seed, "matured", opk=f"pre{i}"), f"pre{i}", f"ev{i}")
        con = sqlite3.connect(str(db))
        con.execute(
            "UPDATE lifecycle_outbox SET object_id=? WHERE operation_key=?", (f"pre{i}", f"pre{i}")
        )
        con.commit()
        con.close()
    gate, reached = threading.Event(), threading.Event()
    winner = _coord(_BarrierClient(client, gate, reached), db, pending_cap=cap)
    loser = _coord(client, db, pending_cap=cap)
    out: dict[str, Any] = {}

    def _run_winner() -> None:
        out["winner"] = winner.transition(_intent(seed, "matured", opk="winner"))

    thread = threading.Thread(target=_run_winner)
    thread.start()
    assert reached.wait(timeout=20), "winner never reached the mutation boundary"
    # Winner admitted the cap-th non-terminal row; the loser is over the SAME production cap.
    loser_res = loser.transition(_intent(seed, "demoted", opk="loser"))
    assert isinstance(loser_res, Err)
    assert loser_res.error.code == "cap_exceeded"
    gate.set()
    thread.join(timeout=20)
    assert isinstance(out["winner"], Ok) and out["winner"].value.kind == "final"


def test_concurrent_same_key_one_wins_other_replays(env: tuple[QdrantClient, _Seed, Path]) -> None:
    # Two identical operation_key + digest callers: one wins; the loser hits the operation_key PK
    # and RE-RESOLVES the winner's stable outcome (never durable_begin_failed), with no double
    # apply/event (integrity hole 4).
    client, seed, db = env
    gate, reached = threading.Event(), threading.Event()
    winner = _coord(_BarrierClient(client, gate, reached), db)
    loser = _coord(client, db)
    out: dict[str, Any] = {}

    def _run_winner() -> None:
        out["winner"] = winner.transition(_intent(seed, "matured", opk="same"))

    thread = threading.Thread(target=_run_winner)
    thread.start()
    assert reached.wait(timeout=20)
    loser_res = loser.transition(_intent(seed, "matured", opk="same"))  # same key + same digest
    assert isinstance(loser_res, Ok), f"the same-key loser must replay, got {loser_res}"
    gate.set()
    thread.join(timeout=20)
    assert isinstance(out["winner"], Ok) and out["winner"].value.kind == "final"
    assert _counts(db) == (1, 1)  # exactly one event/marker despite two callers


# --------------------------------------------------------------------------- #
# Wrong-shape discriminators
# --------------------------------------------------------------------------- #


def test_confirm_discriminates_wrong_readbacks(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    c = _coord(client, db)
    intent = _intent(seed, "matured", expected=1)
    patch = _intended_patch(intent)
    good = {**patch, "namespace": seed.namespace, "object_id": seed.object_id}
    assert c._confirm(patch, seed.object_id, seed.namespace, good, 1) == "confirmed"
    # server filter proof: a readback that is not EXACTLY one point is a fence (a client-side
    # read-then-write cannot prove exactly-one under the server fence).
    assert c._confirm(patch, seed.object_id, seed.namespace, good, 0) == "fence"
    assert c._confirm(patch, seed.object_id, seed.namespace, good, 2) == "fence"
    # version-only: version right but STATE wrong -> fence (never confirmed on version alone).
    assert (
        c._confirm(patch, seed.object_id, seed.namespace, {**good, "state": "provisional"}, 1)
        == "fence"
    )
    # wrong namespace / wrong object -> fence.
    assert (
        c._confirm(patch, seed.object_id, seed.namespace, {**good, "namespace": "other/ns"}, 1)
        == "fence"
    )
    assert (
        c._confirm(patch, seed.object_id, seed.namespace, {**good, "object_id": "other-object"}, 1)
        == "fence"
    )
    # partial patch hash: an intended key missing, or present-but-mismatched -> corrupt.
    assert (
        c._confirm(
            patch,
            seed.object_id,
            seed.namespace,
            {k: v for k, v in good.items() if k != "updated_at"},
            1,
        )
        == "corrupt"
    )
    assert (
        c._confirm(patch, seed.object_id, seed.namespace, {**good, "updated_epoch": -1.0}, 1)
        == "corrupt"
    )
    # sanity: the projected SHA over the correct actual equals the intended SHA.
    assert _canonical_patch_sha({k: good[k] for k in patch}) == _canonical_patch_sha(patch)


def test_stale_version_fences_server_side(env: tuple[QdrantClient, _Seed, Path]) -> None:
    # The fenced set_payload matches zero points when the object is not at expected_version, so a
    # stale intent is a terminal version fence and NEVER mutates the object.
    client, seed, db = env
    assert isinstance(_coord(client, db).transition(_intent(seed, "matured", opk="first")), Ok)
    stale = _coord(client, db).transition(
        _intent(seed, "demoted", opk="stale", expected=seed.version)
    )
    assert isinstance(stale, Err)
    assert stale.error.code == "version_fence_violation"
    assert _qdrant_state(client, seed) == (seed.version + 1, "matured")  # unchanged by the stale op
    assert _outbox(db, "stale")[0] == "ABANDONED"


def test_duplicate_operation_key_replay_is_idempotent(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    client, seed, db = env
    c = _coord(client, db)
    first = c.transition(_intent(seed, "matured", opk="op"))
    assert isinstance(first, Ok) and first.value.kind == "final"
    events_after_first, markers_after_first = _counts(db)
    replay = c.transition(_intent(seed, "matured", opk="op"))  # exact retry
    assert isinstance(replay, Ok) and replay.value.kind == "final"
    assert replay.value.event_id == first.value.event_id  # stable event_id
    assert _counts(db) == (events_after_first, markers_after_first)  # no second event/marker
    # a DIFFERENT intent on the same key is a conflict, not a replay.
    conflict = c.transition(_intent(seed, "demoted", opk="op"))
    assert isinstance(conflict, Err) and conflict.error.code == "operation_key_conflict"


def test_terminal_vs_transient_apply_classification(env: tuple[QdrantClient, _Seed, Path]) -> None:
    client, seed, db = env
    # a KNOWN-terminal apply failure -> ABANDONED + terminal_apply_failure.
    term = _coord(_FailingClient(client, _TerminalErr("proven")), db).transition(
        _intent(seed, "matured", opk="term")
    )
    assert isinstance(term, Err) and term.error.code == "terminal_apply_failure"
    assert _outbox(db, "term")[0] == "ABANDONED"
    # a transient/unknown apply failure is NEVER abandoned -> PENDING for the S4 reconciler.
    trans = _coord(_FailingClient(client, _TransientErr("blip")), db).transition(
        _intent(seed, "matured", opk="trans")
    )
    assert isinstance(trans, Ok) and trans.value.kind == "pending"
    assert _outbox(db, "trans")[0] == "PENDING"


def test_finalize_fault_is_atomic_and_post_commit_crash_truth(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    # A fault INSIDE the finalize txn rolls the event insert back too: the row is EXACTLY APPLIED
    # (mutation durable), no FINAL, no orphaned event; the caller sees Pending (not Final).
    client, seed, db = env
    c = _coord(client, db)

    def _fault(name: str) -> None:
        if name == "inside_finalize_after_event_insert":
            raise RuntimeError("injected finalize-txn fault")

    c._checkpoint = _fault
    res = c.transition(_intent(seed, "matured", opk="op"))
    assert isinstance(res, Ok) and res.value.kind == "pending"  # never Final for an un-finalized op
    assert _outbox(db, "op")[0] == "APPLIED"
    events, markers = _counts(db)
    assert events == 0, "a rolled-back finalize leaves NO event"
    assert markers == 1, "the apply marker + APPLIED are durable"
    assert _qdrant_state(client, seed) == (seed.version + 1, "matured")  # mutation is durable


# --------------------------------------------------------------------------- #
# Private collection -> object_type mapping parity (Yua S3 micro-ruling)
# --------------------------------------------------------------------------- #


def test_collection_object_type_mapping_matches_canonical() -> None:
    from musubi.lifecycle import transitions

    assert coord_mod._COLLECTION_TO_OBJECT_TYPE == transitions._COLLECTION_TO_OBJECT_TYPE, (
        "the coordinator's private mapping must stay in parity with the canonical source"
    )
    # fail-closed: an unknown collection is a pre-mutation terminal validation error.
    with pytest.raises(coord_mod._TerminalValidation):
        coord_mod._object_type_for_collection("musubi_unknown")


# --------------------------------------------------------------------------- #
# Same-key / full-cap TOCTOU regression (Yua 11:47 withhold at d44d051)
# --------------------------------------------------------------------------- #


def _run_full_cap_toctou(
    env: tuple[QdrantClient, _Seed, Path], loser_target: str
) -> tuple[Any, Path, QdrantClient, _Seed]:
    """Deterministic (no-sleep) same-key/full-cap TOCTOU harness. The loser (``client=None`` — so it
    makes ZERO Qdrant calls) passes ``_replay(None)`` and pauses at before_pending_commit BEFORE it
    acquires the write lock; the winner then commits the SOLE ``pending_cap=1`` slot (paused at the
    mutation boundary); the loser is released so its SERIALIZED in-transaction re-check resolves it
    (never the cap); finally the winner is released to Final. Returns the loser outcome + db/client/
    seed. A loser_target equal to the winner's (matured) is an identical replay; a different one
    (e.g. demoted) is a stored_digest conflict — both flow through the in-txn ``_AlreadyExists``."""
    client, seed, db = env
    loser_reached, loser_gate = threading.Event(), threading.Event()

    def _loser_cp(name: str) -> None:
        if name == "before_pending_commit":
            loser_reached.set()
            if not loser_gate.wait(timeout=20):
                raise RuntimeError("loser gate never released")

    loser = _coord(None, db, pending_cap=1)  # client=None: the loser must never touch Qdrant
    loser._checkpoint = _loser_cp
    out: dict[str, Any] = {}

    def _run_loser() -> None:
        out["loser"] = loser.transition(_intent(seed, loser_target, opk="same"))

    lt = threading.Thread(target=_run_loser)
    lt.start()
    assert loser_reached.wait(timeout=20), "loser never reached the admission boundary"

    gate, reached = threading.Event(), threading.Event()
    winner = _coord(_BarrierClient(client, gate, reached), db, pending_cap=1)

    def _run_winner() -> None:
        out["winner"] = winner.transition(_intent(seed, "matured", opk="same"))

    wt = threading.Thread(target=_run_winner)
    wt.start()
    assert reached.wait(timeout=20), "winner never committed its PENDING slot"

    loser_gate.set()  # release the loser into its serialized in-txn re-check
    lt.join(timeout=20)
    gate.set()  # release the winner to Final
    wt.join(timeout=20)
    assert isinstance(out["winner"], Ok) and out["winner"].value.kind == "final"
    return out["loser"], db, client, seed


def test_same_key_full_cap_toctou_replays_before_cap(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    # An IDENTICAL loser (same digest) that already passed _replay(None) must replay the winner
    # (Pending/Final) under a full cap — NEVER cap_exceeded (idempotency-before-cap under contention).
    loser, db, client, seed = _run_full_cap_toctou(env, "matured")
    assert isinstance(loser, Ok), f"same-key loser must replay under a full cap, got {loser}"
    assert loser.value.kind in ("pending", "final")
    assert _counts(db) == (1, 1)  # zero duplicate mutation/event/marker
    assert _qdrant_state(client, seed) == (seed.version + 1, "matured")


def test_conflicting_key_full_cap_toctou_returns_conflict(
    env: tuple[QdrantClient, _Seed, Path],
) -> None:
    # A DIFFERENT-intent loser that ALSO passed _replay(None) and paused before BEGIN: its in-txn
    # re-check sees the winner's row with a DIFFERENT stored_digest -> operation_key_conflict, NOT
    # cap_exceeded (discriminates the stored_digest handling inside _AlreadyExists). Zero Qdrant
    # (client=None); still exactly one row/event/marker.
    loser, db, client, seed = _run_full_cap_toctou(
        env, "demoted"
    )  # demoted != the winner's matured
    assert isinstance(loser, Err), f"conflicting loser must be Err, got {loser}"
    assert loser.error.code == "operation_key_conflict"
    assert _counts(db) == (1, 1)
    assert _qdrant_state(client, seed) == (seed.version + 1, "matured")
