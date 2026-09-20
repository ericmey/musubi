"""Round 29 removed anchor CREATION from `publish()`. This is what holds it removed.

The removal is the safety property of the whole slice: an anchor this update path creates
is an anchor it can resurrect after a retraction, so `publish()` on a genuinely absent
identity must fail rather than write. Copilot round 31 on musubi#732 observed that the
new terminal branch shipped with **no regression test at all** -- correct, and worse than
it looks: neither terminal identity exception had one.

Three things have to hold together, and each fails independently:

1. **Nothing is created.** Zero rows for the object afterwards, counted POSITIVELY by
   `point_kind` rather than as not-something -- a count defined by exclusion silently
   absorbs whatever kind appears next.
2. **Staged content is removed.** The publish writes a content point BEFORE it discovers
   the identity is missing. An orphan snapshot left behind is invisible to every
   anchor-aware read and accumulates forever.
3. **The durable intent is ABANDONED, not pending.** `_classify` returns ``unknown`` for
   an unmarked exception, and an ``unknown`` is rescheduled FOREVER -- never abandoned by
   attempt count. A publish with no identity to update will never acquire one by waiting,
   so without `terminal = True` the row occupies the outbox until the cap evicts it. This
   is the wire nothing else tests, and it is the one a future refactor breaks silently:
   every other assertion here still passes if the marking is lost.

`ImmutableVectorIdentityAmbiguous` carries the identical contract from the identical
reader, so it is covered in the same pass. The round named ONE branch; the contract has
a set of two.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap
from musubi.store.immutable_vectors import (
    ANCHOR_KIND,
    CONTENT_KIND,
    ImmutableVectorIdentityAbsent,
    ImmutableVectorIdentityAmbiguous,
    ImmutableVectorPublisher,
    ImmutableVectorPublishPending,
    register_immutable_vector_dispatch,
)
from musubi.store.names import collection_for_plane
from musubi.store.specs import POINT_KIND_FIELD

_COLL = collection_for_plane("episodic")
_NS = "eric/claude-code/episodic"


@pytest.fixture
def wired(tmp_path: Path) -> Any:
    """A real coordinator + publisher over an in-memory Qdrant, nothing mocked."""
    client = QdrantClient(":memory:")
    bootstrap(client)
    coordinator = LifecycleTransitionCoordinator(client=client, db_path=tmp_path / "coord.db")
    publisher = ImmutableVectorPublisher(client=client, embedder=FakeEmbedder(), collection=_COLL)
    register_immutable_vector_dispatch(coordinator, {_COLL: publisher})
    try:
        yield client, coordinator, publisher
    finally:
        client.close()


def _rows(client: QdrantClient, object_id: str) -> list[Any]:
    found, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=32,
        with_payload=True,
        with_vectors=False,
    )
    return list(found)


def _kinds(client: QdrantClient, object_id: str) -> list[str]:
    """Every row's kind, named POSITIVELY. A legacy row has no `point_kind` and reads
    as ``<legacy>`` rather than being folded into some other bucket."""
    return sorted(
        str((r.payload or {}).get(POINT_KIND_FIELD, "<legacy>")) for r in _rows(client, object_id)
    )


def _outbox(db: Path, object_id: str) -> list[tuple[str, str]]:
    con = sqlite3.connect(db)
    try:
        return [
            (str(s), str(f))
            for s, f in con.execute(
                "SELECT state, COALESCE(failure_class,'') FROM lifecycle_outbox WHERE object_id = ?",
                (object_id,),
            )
        ]
    finally:
        con.close()


# -- 1. the absent case, end to end ------------------------------------------------------------ #


def test_publish_on_absent_identity_creates_nothing_and_abandons(
    wired: Any, tmp_path: Path
) -> None:
    client, coordinator, publisher = wired
    oid = "3JbNOCREATEABSENTOBJECT001"

    assert _rows(client, oid) == [], "precondition: the object must genuinely not exist"

    with pytest.raises(ImmutableVectorPublishPending):
        publisher.publish(
            coordinator,
            object_id=oid,
            namespace=_NS,
            content_payload={
                "content": "body that would have created an anchor",
                "state": "matured",
            },
        )

    # (1) nothing created, and (2) no staged content orphaned -- one assertion covers both
    # only because it is an equality against the empty set.
    assert _kinds(client, oid) == [], (
        f"publish left rows behind for an absent identity: {_kinds(client, oid)}. "
        f"An {ANCHOR_KIND!r} here is a resurrectable anchor; a {CONTENT_KIND!r} here is an "
        "orphan snapshot no anchor-aware read will ever see or clean up."
    )

    # (3) the durable intent is terminal, not waiting for a world that will never arrive.
    states = _outbox(tmp_path / "coord.db", oid)
    assert states == [("ABANDONED", "terminal")], (
        f"durable intent for an absent identity is {states}, expected [('ABANDONED','terminal')]. "
        "PENDING here means the row reschedules forever: `_classify` treats an unmarked "
        "exception as `unknown`, and an `unknown` is never abandoned by attempt count."
    )


def test_absent_exception_is_marked_terminal_for_the_classifier(wired: Any) -> None:
    """The wire that makes the abandon above possible, asserted where it is READ.

    Every other assertion in this file still passes if `terminal` is lost and the outbox
    row merely takes longer to look wrong, so the marking gets its own cell -- and it is
    checked through `_classify`, the consumer, not by reading the attribute back off the
    class that declares it.
    """
    _, coordinator, _ = wired
    assert ImmutableVectorIdentityAbsent.terminal is True
    assert coordinator._classify(ImmutableVectorIdentityAbsent("no identity")) == "terminal"


# -- 2. the sibling the round did not name ----------------------------------------------------- #


def test_publish_on_ambiguous_identity_writes_nothing_and_abandons(
    wired: Any, tmp_path: Path
) -> None:
    """Two identity rows: the publisher must refuse, terminally, without writing either.

    `_read_unique_identity_record` uses ``limit=2`` precisely so ambiguity is observable --
    a ``limit=1`` read returns byte-identical results for one row and for two. Both rows
    here are legacy-shaped (no `point_kind`), which is what the authoritative filter
    selects once content snapshots are excluded.

    **WHAT THIS CELL CANNOT TELL YOU, measured rather than assumed.** TWO independent
    guards make this path terminal: `ImmutableVectorIdentityAmbiguous.terminal` in the
    publisher, and the ``held_count > 1`` cardinality preflight in
    `coordinator._drive_custom_intent`. Removing EITHER one alone leaves this cell green;
    it went red only with both removed. So it asserts the production property -- the
    disjunction -- and it is not a falsifier for either guard on its own. The marking is
    isolated by `test_ambiguous_exception_is_marked_terminal_for_the_classifier`, and the
    preflight by `tests/lifecycle/test_custom_intent_seam.py`. Do not read a pass here as
    evidence that either mechanism survives a refactor.
    """
    client, coordinator, publisher = wired
    oid = "3JbNOCREATEAMBIGUOUSOBJ001"

    before = _seed_two_identity_rows(client, oid)
    assert len(before) == 2, f"precondition: ambiguity must actually exist, got {before}"

    with pytest.raises(ImmutableVectorPublishPending):
        publisher.publish(
            coordinator,
            object_id=oid,
            namespace=_NS,
            content_payload={
                "content": "a body that must not be written anywhere",
                "state": "matured",
            },
        )

    after = {r.id: dict(r.payload or {}) for r in _rows(client, oid)}
    assert after == before, (
        "an ambiguous identity was modified. Neither row may be written: the publisher "
        "cannot know which one is authoritative, and picking either fans the write out."
    )

    states = _outbox(tmp_path / "coord.db", oid)
    assert states == [("ABANDONED", "terminal")], (
        f"durable intent for an ambiguous identity is {states}. Ambiguity does not resolve "
        "itself, so rescheduling is an intent that can never finalize."
    )


def test_ambiguous_exception_is_marked_terminal_for_the_classifier(wired: Any) -> None:
    _, coordinator, _ = wired
    assert ImmutableVectorIdentityAmbiguous.terminal is True
    assert coordinator._classify(ImmutableVectorIdentityAmbiguous("two rows")) == "terminal"


# -- 3. the exit this file CLAIMED to cover and did not ---------------------------------------- #


def test_staged_content_is_removed_when_the_routing_reread_itself_raises(
    wired: Any, tmp_path: Path
) -> None:
    """The object is absent at the fresh read, then TWO identities appear before the route.

    This is the case the first cell's "nothing orphaned" claim did not reach. That cell
    exercises the path where the re-read SUCCEEDS and returns ``None``; here the re-read
    raises `ImmutableVectorIdentityAmbiguous`, which with cleanup ordered after the route
    orphans the staged snapshot -- invisible to every anchor-aware read, collected by
    nothing (Copilot round 32 on musubi#732, "previously missed": the code had not changed).

    The window is real, not contrived: the branch exists precisely BECAUSE another writer
    can create the identity between this intent's fresh read and its publish. Two writers
    doing that is the same race, once more.
    """
    client, coordinator, publisher = wired
    oid = "3JbNOCREATEREREADRAISES001"
    assert _rows(client, oid) == [], "precondition: absent at the fresh read"

    real_upsert = client.upsert
    planted = {"done": False}

    def upsert_then_plant_two_identities(**kwargs: Any) -> Any:
        """Plant the ambiguity AFTER this operation's content point is staged.

        Hooking the upsert is what puts the plant inside the window. Seeding beforehand
        would fail the precondition and take the publisher down a different branch
        entirely -- it would never stage content, so there would be nothing to orphan and
        the cell would pass while measuring nothing.
        """
        result = real_upsert(**kwargs)
        points = kwargs.get("points") or []
        staged_content = any(
            (getattr(p, "payload", None) or {}).get(POINT_KIND_FIELD) == CONTENT_KIND
            and (getattr(p, "payload", None) or {}).get("object_id") == oid
            for p in points
        )
        if staged_content and not planted["done"]:
            planted["done"] = True
            _seed_two_identity_rows(client, oid)
        return result

    client.upsert = upsert_then_plant_two_identities
    try:
        with pytest.raises(ImmutableVectorPublishPending):
            publisher.publish(
                coordinator,
                object_id=oid,
                namespace=_NS,
                content_payload={"content": "staged, then stranded", "state": "matured"},
            )
    finally:
        client.upsert = real_upsert

    assert planted["done"], (
        "the plant never fired, so this cell did not reach the window it exists for -- "
        "it would have passed without ever staging content"
    )
    kinds = _kinds(client, oid)
    assert CONTENT_KIND not in kinds, (
        f"staged content was orphaned when the routing re-read raised: {kinds}. Cleanup "
        "must happen BEFORE the route, so no post-staging exit can skip it."
    )
    assert kinds == ["<legacy>", "<legacy>"], (
        f"expected only the two planted identity rows to remain, got {kinds}"
    )


def _seed_two_identity_rows(client: QdrantClient, object_id: str) -> dict[Any, dict[str, Any]]:
    """Plant exactly two authoritative-looking rows for one (namespace, object_id)."""
    info = client.get_collection(collection_name=_COLL)
    vectors = info.config.params.vectors
    assert isinstance(vectors, dict), "expected the named-vector layout this collection uses"
    name, spec = next(iter(vectors.items()))
    zero = [0.0] * int(spec.size)
    points = [
        models.PointStruct(
            id=f"00000000-0000-4000-8000-00000000000{n}",
            payload={
                "object_id": object_id,
                "namespace": _NS,
                "content": f"legacy row {n}",
                "version": 1,
            },
            vector={name: zero},
        )
        for n in (1, 2)
    ]
    client.upsert(collection_name=_COLL, points=points)
    return {r.id: dict(r.payload or {}) for r in _rows(client, object_id)}
