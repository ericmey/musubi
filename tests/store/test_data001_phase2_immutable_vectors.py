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
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME

pytestmark = (
    pytest.mark.integration
)  # real-Qdrant concurrency; deselected locally without the stack

_NS = "eric/data001p2/episodic"


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    port = os.environ.get("MUSUBI_TEST_QDRANT_PORT")
    client = QdrantClient(host="localhost", port=int(port)) if port else QdrantClient(":memory:")
    bootstrap(client)

    def _wipe() -> None:
        # Real Qdrant persists across runs — isolate this test's namespace at setup AND teardown so a
        # prior run's staged points never pollute counts (RET-004 lesson).
        client.delete(
            collection_name=collection_for_plane("episodic"),
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(key="namespace", match=models.MatchValue(value=_NS))
                    ]
                )
            ),
        )

    _wipe()
    try:
        yield client
    finally:
        _wipe()
        client.close()


@pytest.fixture
def collection() -> str:
    return collection_for_plane("episodic")


@pytest.fixture
def coord(qdrant: QdrantClient, tmp_path: Path) -> LifecycleTransitionCoordinator:
    # tiny backoff so a 'retry' intent becomes re-drivable almost immediately (cleanup-retry path).
    return LifecycleTransitionCoordinator(
        client=qdrant, db_path=tmp_path / "p2-coord.db", backoff_base_s=0.01, backoff_max_s=0.01
    )


def _publisher(qdrant: QdrantClient, collection: str) -> Any:
    from musubi.store.immutable_vectors import ImmutableVectorPublisher

    return ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=collection)


def _content(text: str) -> dict[str, Any]:
    return {"content": text, "tags": ["p2"]}


def _embed(text: str) -> tuple[list[float], dict[int, float]]:
    """Deterministic FakeEmbedder vectors of the collection's real dims, for direct-upsert fixtures."""
    import asyncio

    e = FakeEmbedder()
    return asyncio.run(e.embed_dense([text]))[0], asyncio.run(e.embed_sparse([text]))[0]


# --------------------------------------------------------------------------------------------------
# 1. Losers can never change a visible vector.
# --------------------------------------------------------------------------------------------------
def test_old_owner_late_write_never_becomes_visible(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    """Serialized-intent invariant (Yua): intents are serialized per object (ux_active_intent), so the
    'loser' is a STALE claim's late write, not a second simultaneous intent. Prove: op A commits; a
    concurrent B admitted WHILE A is active returns already_active without changing A's committed
    content; op B admitted only AFTER A is terminal supersedes A; then a simulated stale-A late publish
    fenced on A's old pointer_version matches zero and CANNOT revert B."""

    pub = _publisher(qdrant, collection)
    pub.register(coord)
    oid = "obj-late-write"
    assert (
        pub.admit_publish(coord, object_id=oid, namespace=_NS, content_payload=_content("A"))
        == "admitted"
    )
    # concurrent B while A is still active -> already_active, and A's identity/content is unmutated.
    assert (
        pub.admit_publish(
            coord, object_id=oid, namespace=_NS, content_payload=_content("B-while-active")
        )
        == "already_active"
    )
    coord.reconcile_once()  # A commits
    assert (resolve_or_none(qdrant, collection, oid) or {})["content"] == "A"
    a_pv = _anchor(qdrant, collection, oid).pointer_version

    # op B is admitted ONLY now that A is terminal, and supersedes.
    assert (
        pub.admit_publish(coord, object_id=oid, namespace=_NS, content_payload=_content("B"))
        == "admitted"
    )
    coord.reconcile_once()  # B commits
    assert (resolve_or_none(qdrant, collection, oid) or {})["content"] == "B"
    b_pv = _anchor(qdrant, collection, oid).pointer_version
    assert b_pv > a_pv

    # a STALE-A late publish, fenced on A's OLD pointer_version, matches zero -> cannot revert B.
    qdrant.set_payload(
        collection_name=collection,
        payload={"live_point": "stale-a-content", "committed_operation_id": "stale-a"},
        points=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=oid)),
                models.FieldCondition(key="point_kind", match=models.MatchValue(value="anchor")),
                models.FieldCondition(key="pointer_version", match=models.MatchValue(value=a_pv)),
            ]
        ),
    )
    assert (resolve_or_none(qdrant, collection, oid) or {})["content"] == "B", (
        "stale-A late write must not revert B"
    )
    assert _anchor(qdrant, collection, oid).pointer_version == b_pv


# --------------------------------------------------------------------------------------------------
# 2. content_point_id derives from the STABLE operation_key, not the per-claim owner_token.
# --------------------------------------------------------------------------------------------------
def test_content_point_id_is_stable_across_reconcile(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.store.immutable_vectors import content_point_id_for

    # Deterministic in the STABLE operation_key (a reconcile re-drive reuses the SAME id), and distinct
    # per operation_key — so it can never derive from the per-claim owner_token (which changes).
    assert content_point_id_for("op-A", 0) == content_point_id_for("op-A", 0)
    assert content_point_id_for("op-A", 0) != content_point_id_for("op-B", 0)
    assert content_point_id_for("op-A", 0) != content_point_id_for("op-A", 1)


# --------------------------------------------------------------------------------------------------
# 3. Replay from disk with NO caller memory (durable intent).
# --------------------------------------------------------------------------------------------------
def test_crash_before_pointer_replays_from_disk(
    qdrant: QdrantClient, collection: str, tmp_path: Path
) -> None:
    """Admit the intent, then reconstruct a FRESH coordinator + publisher from the same db_path/Qdrant
    (no in-memory caller state) and reconcile — it must converge to a published pointer."""
    db = tmp_path / "replay-coord.db"
    coord1 = LifecycleTransitionCoordinator(client=qdrant, db_path=db)
    pub1 = _publisher(qdrant, collection)
    pub1.register(coord1)
    pub1.admit_publish(
        coord1,
        object_id="obj-replay",
        namespace=_NS,
        content_payload=_content("recover"),
    )
    # Simulate crash-before-apply: drop coord1/pub1, rebuild from disk only.
    del coord1, pub1
    coord2 = LifecycleTransitionCoordinator(client=qdrant, db_path=db)
    pub2 = _publisher(qdrant, collection)
    pub2.register(coord2)
    coord2.reconcile_once()

    anchor = _anchor(qdrant, collection, "obj-replay")
    assert anchor is not None and anchor.live_point is not None
    assert (resolve_or_none(qdrant, collection, "obj-replay") or {})["content"] == "recover"


# --------------------------------------------------------------------------------------------------
# 4. Crash-after-pointer: idempotent, no double-apply (operation_key stamped on the anchor).
# --------------------------------------------------------------------------------------------------
def test_crash_after_pointer_no_double_apply(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.admit_publish(
        coord,
        object_id="obj-idem",
        namespace=_NS,
        content_payload=_content("once"),
    )
    coord.reconcile_once()

    v1 = _anchor(qdrant, collection, "obj-idem").version
    # Re-drive the SAME (already-terminal) intent: must be a confirmed no-op, version not re-bumped.
    coord.reconcile_once()
    v2 = _anchor(qdrant, collection, "obj-idem").version
    assert v1 == v2, f"a re-driven committed intent must not double-bump version ({v1} -> {v2})"


# --------------------------------------------------------------------------------------------------
# 5. Cleanup is terminal correctness — failure returns retry, pointer stays attributable.
# --------------------------------------------------------------------------------------------------
def test_cleanup_failure_returns_retry_pointer_stays_attributable(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    # first publish
    pub.admit_publish(
        coord,
        object_id="obj-clean",
        namespace=_NS,
        content_payload=_content("v1"),
    )
    coord.reconcile_once()
    # second publish supersedes; force loser/superseded cleanup to fail once.
    pub.fail_cleanup_once()  # fault-injection seam
    pub.admit_publish(
        coord,
        object_id="obj-clean",
        namespace=_NS,
        content_payload=_content("v2"),
    )
    coord.reconcile_once()  # publishes pointer, cleanup fails -> intent stays PENDING (retry)
    assert (resolve_or_none(qdrant, collection, "obj-clean") or {})["content"] == "v2", (
        "the published pointer must remain attributable even though cleanup failed"
    )
    import time

    time.sleep(0.03)  # let the tiny backoff elapse so the retry is re-drivable
    coord.reconcile_once()  # retry completes cleanup
    assert _count_content_points(qdrant, collection, "obj-clean") == 1, (
        "superseded point GC'd on retry"
    )


# --------------------------------------------------------------------------------------------------
# 6. Composition with the RET-008 access lease.
# --------------------------------------------------------------------------------------------------
def test_concurrent_access_lease_composition(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.store.access_lease import lease_increment_access

    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.admit_publish(
        coord,
        object_id="obj-lease",
        namespace=_NS,
        content_payload=_content("c"),
    )
    coord.reconcile_once()

    before = _anchor(qdrant, collection, "obj-lease")
    # an access increment on the anchor + a subsequent vector publish must BOTH survive
    import asyncio

    asyncio.run(lease_increment_access(qdrant, collection, {(_NS, "obj-lease")}))
    pub.admit_publish(
        coord,
        object_id="obj-lease",
        namespace=_NS,
        content_payload=_content("c2"),
    )
    coord.reconcile_once()
    after = _anchor(qdrant, collection, "obj-lease")
    assert after.access_count >= before.access_count + 1, (
        "access_count must not be clobbered by the swap"
    )
    assert (resolve_or_none(qdrant, collection, "obj-lease") or {})["content"] == "c2"


# --------------------------------------------------------------------------------------------------
# 7. No-future-mutation orphan reconciled.
# --------------------------------------------------------------------------------------------------
def test_no_future_mutation_orphan_reconciled(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.stall_after_staging_once()  # owner stages a content point then never returns
    pub.admit_publish(
        coord,
        object_id="obj-orphan",
        namespace=_NS,
        content_payload=_content("o"),
    )
    coord.reconcile_once()  # stalls after staging
    coord.reconcile_once()  # reconcile completes-or-cleans the orphan
    assert _count_content_points(qdrant, collection, "obj-orphan") <= 1, (
        "orphan staged point reconciled"
    )


# --------------------------------------------------------------------------------------------------
# 8. Reads follow only the committed pointer.
# --------------------------------------------------------------------------------------------------
def test_read_follows_committed_pointer_only(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.admit_publish(
        coord,
        object_id="obj-read",
        namespace=_NS,
        content_payload=_content("old"),
    )
    coord.reconcile_once()
    pub.admit_publish(
        coord,
        object_id="obj-read",
        namespace=_NS,
        content_payload=_content("new"),
    )
    coord.reconcile_once()
    # the un-pointed (old) content point must never be returned by the pointer-resolving read
    assert (resolve_or_none(qdrant, collection, "obj-read") or {})["content"] == "new"


# --------------------------------------------------------------------------------------------------
# 9. The anchor's zero vector never ranks in a vector search.
# --------------------------------------------------------------------------------------------------
def test_anchor_never_ranks_in_vector_search(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.admit_publish(
        coord,
        object_id="obj-rank",
        namespace=_NS,
        content_payload=_content("body"),
    )
    coord.reconcile_once()
    from musubi.store.immutable_vectors import ANCHOR_KIND

    # a vector query must return content points only — anchors are excluded by kind + zero vector
    d, _ = _embed("body")
    res = qdrant.query_points(
        collection_name=collection, query=d, using=DENSE_VECTOR_NAME, limit=10, with_payload=True
    ).points
    assert all((p.payload or {}).get("point_kind") != ANCHOR_KIND for p in res), (
        "anchor must not rank"
    )


# --------------------------------------------------------------------------------------------------
# 10. Layout versioning: v1 legacy self-pointer served; v2 missing pointer fails closed.
# --------------------------------------------------------------------------------------------------
def test_legacy_v1_served_as_self_pointer_and_v2_missing_pointer_fails_closed(
    qdrant: QdrantClient, collection: str
) -> None:
    import uuid

    from musubi.store.immutable_vectors import resolve_committed_content

    d, s = _embed("legacy")
    # v1 legacy single-point row (no anchor/live_point) — served as self-pointer.
    qdrant.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                payload={
                    "object_id": "obj-v1",
                    "namespace": _NS,
                    "content": "legacy",
                    "state": "matured",
                },
                vector={
                    DENSE_VECTOR_NAME: d,
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(s), values=list(s.values())
                    ),
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
                id=str(uuid.uuid4()),
                payload={
                    "object_id": "obj-v2",
                    "namespace": _NS,
                    "vector_layout_version": 2,
                    "point_kind": "anchor",
                },
                vector={
                    DENSE_VECTOR_NAME: [0.0] * len(d),
                    SPARSE_VECTOR_NAME: models.SparseVector(indices=[], values=[]),
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
def test_first_vector_mutation_bootstraps_v1_to_v2(
    qdrant: QdrantClient, collection: str, coord: LifecycleTransitionCoordinator
) -> None:
    import uuid

    d0, s0 = _embed("legacy-body")
    qdrant.upsert(
        collection_name=collection,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                payload={
                    "object_id": "obj-boot",
                    "namespace": _NS,
                    "content": "legacy-body",
                    "state": "matured",
                },
                vector={
                    DENSE_VECTOR_NAME: d0,
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(s0), values=list(s0.values())
                    ),
                },
            )
        ],
    )
    pub = _publisher(qdrant, collection)
    pub.register(coord)
    pub.admit_publish(
        coord,
        object_id="obj-boot",
        namespace=_NS,
        content_payload=_content("new-body"),
    )
    coord.reconcile_once()
    anchor = _anchor(qdrant, collection, "obj-boot")
    assert (
        anchor is not None and anchor.vector_layout_version == 2 and anchor.live_point is not None
    )
    assert (resolve_or_none(qdrant, collection, "obj-boot") or {})["content"] == "new-body"


# --------------------------------------------------------------------------------------------------
# helpers (bind the intended read/resolve + counting surface)
# --------------------------------------------------------------------------------------------------
def resolve_or_none(qdrant: QdrantClient, collection: str, object_id: str) -> dict[str, Any] | None:
    from musubi.store.immutable_vectors import resolve_committed_content

    return resolve_committed_content(qdrant, collection, namespace=_NS, object_id=object_id)


def _anchor(qdrant: QdrantClient, collection: str, object_id: str) -> Any:
    from musubi.store.immutable_vectors import read_anchor

    a = read_anchor(qdrant, collection, namespace=_NS, object_id=object_id)
    assert a is not None
    return a


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
