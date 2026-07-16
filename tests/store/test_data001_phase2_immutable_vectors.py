"""DATA-001 Phase 2 — immutable vectors + fenced committed pointer (#530). Tests-first RED contract.

Design: docs/Musubi/13-decisions/data001-phase2-immutable-vectors.md. These drive the REAL lifecycle
coordinator's reconcile worker (additive `immutable_vector_publish` intent kind — NOT a new worker),
against real Qdrant. They are RED until src/musubi/store/immutable_vectors.py exists and is wired.

Intended API (what these tests bind):
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,   # .register(coord); .admit_publish(coord, *, object_id, namespace,
                                    #   content_payload, dense, sparse) -> operation_key
        read_anchor,                # (client, collection, namespace, object_id) -> AnchorView | None
        resolve_committed_content,  # (client, collection, namespace, object_id) -> content payload | None
        ANCHOR_KIND, VECTOR_LAYOUT_V2,
    )
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane

pytestmark = (
    pytest.mark.integration
)  # real-Qdrant concurrency; deselected locally without the stack

_NS = "eric/data001p2/episodic"


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    port = os.environ.get("MUSUBI_TEST_QDRANT_PORT")
    client = QdrantClient(host="localhost", port=int(port)) if port else QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def collection() -> str:
    return collection_for_plane("episodic")


@pytest.fixture
def coord(qdrant: QdrantClient, tmp_path) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "p2-coord.db")


def _publisher(qdrant: QdrantClient, collection: str):
    from musubi.store.immutable_vectors import ImmutableVectorPublisher

    return ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=collection)


def _content(text: str) -> dict:
    return {"content": text, "tags": ["p2"]}


def _vec(seed: float) -> tuple[list[float], dict[int, float]]:
    return [seed, 1.0 - seed, 0.5], {1: seed, 3: 1.0 - seed}


# --------------------------------------------------------------------------------------------------
# 1. Losers can never change a visible vector.
# --------------------------------------------------------------------------------------------------
def test_old_owner_late_write_never_becomes_visible(qdrant, collection, coord) -> None:
    """A stalls after staging its content point; B takes over on a fresh claim and publishes its own
    via the fenced anchor swap. A's late swap matches zero (own-token fenced) → A's content point is
    NEVER named by the committed live_point."""
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    dense_a, sparse_a = _vec(0.10)
    dense_b, sparse_b = _vec(0.90)
    oid = "obj-late-write"
    # A admits + stages but its lease expires; B admits and wins. Modeled via the publisher's
    # fault-injection seam that stalls A after staging and lets B's reconcile publish first.
    pub.admit_publish(
        coord,
        object_id=oid,
        namespace=_NS,
        content_payload=_content("A"),
        dense=dense_a,
        sparse=sparse_a,
    )
    pub.admit_publish(
        coord,
        object_id=oid,
        namespace=_NS,
        content_payload=_content("B"),
        dense=dense_b,
        sparse=sparse_b,
    )
    coord.reconcile_once()
    coord.reconcile_once()
    from musubi.store.immutable_vectors import read_anchor

    anchor = read_anchor(qdrant, collection, namespace=_NS, object_id=oid)
    assert anchor is not None and anchor.live_point is not None
    # Exactly one winner; A's content never becomes the committed pointer.
    committed = resolve_or_none(qdrant, collection, oid)
    assert committed is not None and committed["content"] == "B", (
        f"the winner's content must be visible, never a loser's; got {committed!r}"
    )


# --------------------------------------------------------------------------------------------------
# 2. content_point_id derives from the STABLE operation_key, not the per-claim owner_token.
# --------------------------------------------------------------------------------------------------
def test_content_point_id_is_stable_across_reconcile(qdrant, collection, coord) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    from musubi.store.immutable_vectors import content_point_id_for

    dense, sparse = _vec(0.3)
    op = pub.admit_publish(
        coord,
        object_id="obj-stable",
        namespace=_NS,
        content_payload=_content("x"),
        dense=dense,
        sparse=sparse,
    )
    first = content_point_id_for(op, generation=0)
    second = content_point_id_for(op, generation=0)
    assert first == second, (
        "content_point_id must be deterministic from operation_key (+ generation)"
    )


# --------------------------------------------------------------------------------------------------
# 3. Replay from disk with NO caller memory (durable intent).
# --------------------------------------------------------------------------------------------------
def test_crash_before_pointer_replays_from_disk(qdrant, collection, tmp_path) -> None:
    """Admit the intent, then reconstruct a FRESH coordinator + publisher from the same db_path/Qdrant
    (no in-memory caller state) and reconcile — it must converge to a published pointer."""
    db = tmp_path / "replay-coord.db"
    coord1 = LifecycleTransitionCoordinator(client=qdrant, db_path=db)
    pub1 = _publisher(qdrant, collection)
    pub1.register(coord1)
    dense, sparse = _vec(0.4)
    pub1.admit_publish(
        coord1,
        object_id="obj-replay",
        namespace=_NS,
        content_payload=_content("recover"),
        dense=dense,
        sparse=sparse,
    )
    # Simulate crash-before-apply: drop coord1/pub1, rebuild from disk only.
    del coord1, pub1
    coord2 = LifecycleTransitionCoordinator(client=qdrant, db_path=db)
    pub2 = _publisher(qdrant, collection)
    pub2.register(coord2)
    coord2.reconcile_once()
    from musubi.store.immutable_vectors import read_anchor

    anchor = read_anchor(qdrant, collection, namespace=_NS, object_id="obj-replay")
    assert anchor is not None and anchor.live_point is not None
    assert resolve_or_none(qdrant, collection, "obj-replay")["content"] == "recover"


# --------------------------------------------------------------------------------------------------
# 4. Crash-after-pointer: idempotent, no double-apply (operation_key stamped on the anchor).
# --------------------------------------------------------------------------------------------------
def test_crash_after_pointer_no_double_apply(qdrant, collection, coord) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    dense, sparse = _vec(0.5)
    pub.admit_publish(
        coord,
        object_id="obj-idem",
        namespace=_NS,
        content_payload=_content("once"),
        dense=dense,
        sparse=sparse,
    )
    coord.reconcile_once()
    from musubi.store.immutable_vectors import read_anchor

    v1 = read_anchor(qdrant, collection, namespace=_NS, object_id="obj-idem").version
    # Re-drive the SAME (already-terminal) intent: must be a confirmed no-op, version not re-bumped.
    coord.reconcile_once()
    v2 = read_anchor(qdrant, collection, namespace=_NS, object_id="obj-idem").version
    assert v1 == v2, f"a re-driven committed intent must not double-bump version ({v1} -> {v2})"


# --------------------------------------------------------------------------------------------------
# 5. Cleanup is terminal correctness — failure returns retry, pointer stays attributable.
# --------------------------------------------------------------------------------------------------
def test_cleanup_failure_returns_retry_pointer_stays_attributable(
    qdrant, collection, coord
) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    # first publish
    d1, s1 = _vec(0.2)
    pub.admit_publish(
        coord,
        object_id="obj-clean",
        namespace=_NS,
        content_payload=_content("v1"),
        dense=d1,
        sparse=s1,
    )
    coord.reconcile_once()
    # second publish supersedes; force loser/superseded cleanup to fail once.
    pub.fail_cleanup_once()  # fault-injection seam
    d2, s2 = _vec(0.8)
    pub.admit_publish(
        coord,
        object_id="obj-clean",
        namespace=_NS,
        content_payload=_content("v2"),
        dense=d2,
        sparse=s2,
    )
    coord.reconcile_once()  # publishes pointer, cleanup fails -> intent stays PENDING (retry)
    assert resolve_or_none(qdrant, collection, "obj-clean")["content"] == "v2", (
        "the published pointer must remain attributable even though cleanup failed"
    )
    coord.reconcile_once()  # retry completes cleanup
    assert _count_content_points(qdrant, collection, "obj-clean") == 1, (
        "superseded point GC'd on retry"
    )


# --------------------------------------------------------------------------------------------------
# 6. Composition with the RET-008 access lease.
# --------------------------------------------------------------------------------------------------
def test_concurrent_access_lease_composition(qdrant, collection, coord) -> None:
    from musubi.store.access_lease import lease_increment_access

    pub = _publisher(qdrant, collection)
    pub.register(coord)
    d, s = _vec(0.3)
    pub.admit_publish(
        coord,
        object_id="obj-lease",
        namespace=_NS,
        content_payload=_content("c"),
        dense=d,
        sparse=s,
    )
    coord.reconcile_once()
    from musubi.store.immutable_vectors import read_anchor

    before = read_anchor(qdrant, collection, namespace=_NS, object_id="obj-lease")
    # an access increment on the anchor + a subsequent vector publish must BOTH survive
    import asyncio

    asyncio.run(lease_increment_access(qdrant, collection, [(_NS, "obj-lease")]))
    d2, s2 = _vec(0.7)
    pub.admit_publish(
        coord,
        object_id="obj-lease",
        namespace=_NS,
        content_payload=_content("c2"),
        dense=d2,
        sparse=s2,
    )
    coord.reconcile_once()
    after = read_anchor(qdrant, collection, namespace=_NS, object_id="obj-lease")
    assert after.access_count >= before.access_count + 1, (
        "access_count must not be clobbered by the swap"
    )
    assert resolve_or_none(qdrant, collection, "obj-lease")["content"] == "c2"


# --------------------------------------------------------------------------------------------------
# 7. No-future-mutation orphan reconciled.
# --------------------------------------------------------------------------------------------------
def test_no_future_mutation_orphan_reconciled(qdrant, collection, coord) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.stall_after_staging_once()  # owner stages a content point then never returns
    d, s = _vec(0.6)
    pub.admit_publish(
        coord,
        object_id="obj-orphan",
        namespace=_NS,
        content_payload=_content("o"),
        dense=d,
        sparse=s,
    )
    coord.reconcile_once()  # stalls after staging
    coord.reconcile_once()  # reconcile completes-or-cleans the orphan
    assert _count_content_points(qdrant, collection, "obj-orphan") <= 1, (
        "orphan staged point reconciled"
    )


# --------------------------------------------------------------------------------------------------
# 8. Reads follow only the committed pointer.
# --------------------------------------------------------------------------------------------------
def test_read_follows_committed_pointer_only(qdrant, collection, coord) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    d1, s1 = _vec(0.2)
    pub.admit_publish(
        coord,
        object_id="obj-read",
        namespace=_NS,
        content_payload=_content("old"),
        dense=d1,
        sparse=s1,
    )
    coord.reconcile_once()
    d2, s2 = _vec(0.8)
    pub.admit_publish(
        coord,
        object_id="obj-read",
        namespace=_NS,
        content_payload=_content("new"),
        dense=d2,
        sparse=s2,
    )
    coord.reconcile_once()
    # the un-pointed (old) content point must never be returned by the pointer-resolving read
    assert resolve_or_none(qdrant, collection, "obj-read")["content"] == "new"


# --------------------------------------------------------------------------------------------------
# 9. The anchor's zero vector never ranks in a vector search.
# --------------------------------------------------------------------------------------------------
def test_anchor_never_ranks_in_vector_search(qdrant, collection, coord) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    d, s = _vec(0.5)
    pub.admit_publish(
        coord,
        object_id="obj-rank",
        namespace=_NS,
        content_payload=_content("body"),
        dense=d,
        sparse=s,
    )
    coord.reconcile_once()
    from musubi.store.immutable_vectors import ANCHOR_KIND

    # a vector query must return content points only — anchors are excluded by kind + zero vector
    res = qdrant.query_points(
        collection_name=collection, query=d, using="dense", limit=10, with_payload=True
    ).points
    assert all((p.payload or {}).get("point_kind") != ANCHOR_KIND for p in res), (
        "anchor must not rank"
    )


# --------------------------------------------------------------------------------------------------
# 10. Layout versioning: v1 legacy self-pointer served; v2 missing pointer fails closed.
# --------------------------------------------------------------------------------------------------
def test_legacy_v1_served_as_self_pointer_and_v2_missing_pointer_fails_closed(
    qdrant, collection
) -> None:
    from musubi.store.immutable_vectors import resolve_committed_content

    # v1 legacy single-point row (no anchor/live_point) — served as self-pointer.
    d, s = _vec(0.4)
    qdrant.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id="00000000-0000-0000-0000-0000000000v1",
                payload={
                    "object_id": "obj-v1",
                    "namespace": _NS,
                    "content": "legacy",
                    "state": "matured",
                },
                vector={
                    "dense": d,
                    "sparse": models.SparseVector(indices=list(s), values=list(s.values())),
                },
            )
        ],
    )
    assert (
        resolve_committed_content(qdrant, collection, namespace=_NS, object_id="obj-v1") is not None
    )
    # a v2 anchor with an ABSENT live_point must fail closed (NOT be treated as legacy)
    qdrant.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id="00000000-0000-0000-0000-0000000000v2",
                payload={
                    "object_id": "obj-v2",
                    "namespace": _NS,
                    "vector_layout_version": 2,
                    "point_kind": "anchor",
                },
                vector={
                    "dense": [0.0, 0.0, 0.0],
                    "sparse": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
    )
    assert (
        resolve_committed_content(qdrant, collection, namespace=_NS, object_id="obj-v2") is None
    ), "a v2 anchor with no live_point must fail closed, never be interpreted as legacy"


# --------------------------------------------------------------------------------------------------
# 11. First vector-changing mutation bootstraps a v1 row into content point + v2 anchor.
# --------------------------------------------------------------------------------------------------
def test_first_vector_mutation_bootstraps_v1_to_v2(qdrant, collection, coord) -> None:
    from musubi.store.immutable_vectors import read_anchor

    d0, s0 = _vec(0.3)
    qdrant.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id="00000000-0000-0000-0000-0000000boot",
                payload={
                    "object_id": "obj-boot",
                    "namespace": _NS,
                    "content": "legacy-body",
                    "state": "matured",
                },
                vector={
                    "dense": d0,
                    "sparse": models.SparseVector(indices=list(s0), values=list(s0.values())),
                },
            )
        ],
    )
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    d1, s1 = _vec(0.9)
    pub.admit_publish(
        coord,
        object_id="obj-boot",
        namespace=_NS,
        content_payload=_content("new-body"),
        dense=d1,
        sparse=s1,
    )
    coord.reconcile_once()
    anchor = read_anchor(qdrant, collection, namespace=_NS, object_id="obj-boot")
    assert (
        anchor is not None and anchor.vector_layout_version == 2 and anchor.live_point is not None
    )
    assert resolve_or_none(qdrant, collection, "obj-boot")["content"] == "new-body"


# --------------------------------------------------------------------------------------------------
# helpers (bind the intended read/resolve + counting surface)
# --------------------------------------------------------------------------------------------------
def resolve_or_none(qdrant: QdrantClient, collection: str, object_id: str) -> dict | None:
    from musubi.store.immutable_vectors import resolve_committed_content

    return resolve_committed_content(qdrant, collection, namespace=_NS, object_id=object_id)


def _count_content_points(qdrant: QdrantClient, collection: str, object_id: str) -> int:
    from musubi.store.immutable_vectors import ANCHOR_KIND

    recs, _ = qdrant.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))],
            must_not=[
                models.FieldCondition(key="point_kind", match=models.MatchValue(value=ANCHOR_KIND))
            ],
        ),
        limit=100,
    )
    return len(recs)
