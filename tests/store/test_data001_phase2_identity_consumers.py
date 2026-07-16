"""DATA-001 Phase 2 — identity-consumer (unit A-rest) discriminators (#530).

The multi-point layout is only correct if every identity consumer resolves the anchor. These prove the
A-rest seams against real Qdrant: coordinator/transition identity lookup, namespace-stats count,
API-list scroll, recent, and synthesis clustering — each excludes content shells and fails closed on a
dangling/cross-object committed pointer. Inventory:
docs/Musubi/13-decisions/data001-phase2-identity-consumer-inventory.md.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, POINT_KIND_FIELD

pytestmark = pytest.mark.integration

_NS = "eric/data001p2ic/episodic"
_COLL = collection_for_plane("episodic")


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    port = os.environ.get("MUSUBI_TEST_QDRANT_PORT")
    client = QdrantClient(host="localhost", port=int(port)) if port else QdrantClient(":memory:")
    bootstrap(client)

    def _wipe() -> None:
        client.delete(
            collection_name=_COLL,
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
def coord(qdrant: QdrantClient, tmp_path: Path) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(
        client=qdrant, db_path=tmp_path / "ic-coord.db", backoff_base_s=0.01, backoff_max_s=0.01
    )


def _make_v2(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, oid: str, **fields: Any
) -> None:
    """Publish a v2 object (anchor + content) via the immutable-vector seam."""
    from musubi.store.immutable_vectors import ImmutableVectorPublisher

    pub = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=_COLL)
    pub.register(coord)
    pub.publish(coord, object_id=oid, namespace=_NS, content_payload={"content": oid, **fields})


def _make_dangling(qdrant: QdrantClient, oid: str) -> None:
    """Delete the committed content point of a v2 object, leaving a dangling anchor pointer."""
    from musubi.store.immutable_vectors import read_anchor

    anchor = read_anchor(qdrant, _COLL, namespace=_NS, object_id=oid)
    assert anchor is not None and anchor.live_point is not None
    qdrant.delete(collection_name=_COLL, points_selector=[anchor.live_point])


# --------------------------------------------------------------------------------------------------
# transitions._lookup_point_id / _scroll_by_object_id resolve the IDENTITY row, never a content shell.
# --------------------------------------------------------------------------------------------------
def test_transition_identity_lookup_excludes_content(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.lifecycle.transitions import _lookup_point_id, _scroll_by_object_id
    from musubi.store.immutable_vectors import ANCHOR_KIND, anchor_point_id

    _make_v2(qdrant, coord, "tr-1")
    payloads = _scroll_by_object_id(qdrant, collection=_COLL, object_id="tr-1")
    assert len(payloads) == 1 and payloads[0].get("point_kind") == ANCHOR_KIND
    assert str(_lookup_point_id(qdrant, collection=_COLL, object_id="tr-1")) == anchor_point_id(
        _NS, "tr-1"
    ), "the identity point id must be the anchor, never a content point"


# --------------------------------------------------------------------------------------------------
# The ACTUAL namespace_stats route counts ONE identity per object despite N content points.
# --------------------------------------------------------------------------------------------------
def test_namespace_stats_route_counts_one_identity_per_v2_object(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    from musubi.api.routers import namespaces as ns_mod

    # sanity: the raw namespace scroll sees the anchor + its content point (two points).
    raw = qdrant.count(
        collection_name=_COLL,
        count_filter=models.Filter(
            must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=_NS))]
        ),
        exact=True,
    ).count
    _make_v2(qdrant, coord, "cnt-1")
    assert (
        qdrant.count(
            collection_name=_COLL,
            count_filter=models.Filter(
                must=[models.FieldCondition(key="namespace", match=models.MatchValue(value=_NS))]
            ),
            exact=True,
        ).count
        == raw + 2
    ), "raw count sees the anchor + its content point"

    # invoke the REAL route (auth no-op'd) so a production revert of the identity exclusion fails here.
    monkeypatch.setattr(ns_mod, "authorize_namespace", lambda *a, **k: None)
    result = asyncio.run(
        ns_mod.namespace_stats(
            request=MagicMock(), namespace_path=_NS, qdrant=qdrant, settings=MagicMock()
        )
    )
    assert result.counts["episodic"] == 1, (
        "namespace_stats must count exactly one identity per v2 object, not the content points"
    )


# --------------------------------------------------------------------------------------------------
# API list scroll: excludes content, resolves identity, dangling underfills, cursor stays truthful.
# --------------------------------------------------------------------------------------------------
def test_scroll_namespace_excludes_content_and_underfills_on_dangling(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.api.routers._scroll import scroll_namespace

    _make_v2(qdrant, coord, "sc-ok")
    _make_v2(qdrant, coord, "sc-dangle")
    _make_dangling(qdrant, "sc-dangle")

    items, cursor = scroll_namespace(qdrant, collection=_COLL, namespace=_NS, limit=10, cursor=None)
    ids = {i.get("object_id") for i in items}
    assert "sc-ok" in ids, "a healthy v2 object is resolved + listed"
    assert "sc-dangle" not in ids, "a dangling pointer fails closed (underfills the page)"
    assert all(POINT_KIND_FIELD not in i for i in items), (
        "no content/anchor layout keys leak into a list item"
    )
    assert cursor is None, (
        "a fully-drained page keeps a truthful (no more pages) cursor despite underfill"
    )


def test_scroll_namespace_cursor_survives_dangling_underfill() -> None:
    """A page whose only identity row is a dangling pointer must return items=[] YET carry forward the
    exact next_offset Qdrant gave, so pagination continues (Yua): the underfill must not truncate the
    cursor. Uses a controlled stub so next_offset is non-None."""
    from musubi.api.routers._scroll import _decode_offset, scroll_namespace

    anchor_payload = {
        "object_id": "dang-1",
        "namespace": _NS,
        "point_kind": "anchor",
        "live_point": "cpX",
    }
    anchor_point = type("P", (), {"payload": anchor_payload, "id": "anchor-id"})()

    class _CursorStub:
        def scroll(self, **kw: Any) -> tuple[list[Any], Any]:
            f = kw.get("scroll_filter")
            must = getattr(f, "must", None) or []
            is_anchor_scroll = any(
                getattr(c, "key", "") == "point_kind"
                and getattr(getattr(c, "match", None), "value", None) == "anchor"
                for c in must
            )
            # resolve()'s anchor scroll returns the anchor; the primary list scroll also returns a
            # non-None next_offset so we can prove it survives the resolver dropping the dangling row.
            return ([anchor_point], None if is_anchor_scroll else "NEXT-OFFSET")

        def retrieve(self, **kw: Any) -> list[Any]:
            return []  # the committed content point is gone -> dangling

    items, cursor = scroll_namespace(
        cast(QdrantClient, _CursorStub()), collection=_COLL, namespace=_NS, limit=5, cursor=None
    )
    assert items == [], "the dangling row fails closed and underfills the page"
    assert cursor is not None and _decode_offset(cursor) == "NEXT-OFFSET", (
        "the exact next_offset must survive the underfill so pagination continues"
    )


# --------------------------------------------------------------------------------------------------
# recent: v2 returns resolved committed content; dangling skips; non-anchor unaffected.
# --------------------------------------------------------------------------------------------------
def test_recent_resolves_v2_and_skips_dangling(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.retrieve.recent import run_recent_retrieve
    from musubi.types.common import Ok

    _make_v2(qdrant, coord, "rc-ok", state="matured", created_epoch=2.0)
    _make_v2(qdrant, coord, "rc-dangle", state="matured", created_epoch=1.0)
    _make_dangling(qdrant, "rc-dangle")

    res = asyncio.run(run_recent_retrieve(client=qdrant, namespace=_NS, collection=_COLL, limit=10))
    assert isinstance(res, Ok)
    ids = {h.object_id for h in res.value.results}
    assert "rc-ok" in ids and "rc-dangle" not in ids
    ok_hit = next(h for h in res.value.results if h.object_id == "rc-ok")
    assert ok_hit.payload.get("content") == "rc-ok", (
        "the hit projects the RESOLVED committed content"
    )
    assert POINT_KIND_FIELD not in ok_hit.payload, "no layout keys leak into a recent hit"


# --------------------------------------------------------------------------------------------------
# synthesis: the anchor-aware resolver is a SINGLE consistent read — no torn payload/vector under swap.
# --------------------------------------------------------------------------------------------------
def test_synthesis_resolve_candidate_no_torn_read() -> None:
    from musubi.lifecycle.synthesis import _resolve_candidate_memory
    from musubi.types.episodic import EpisodicMemory

    mem = EpisodicMemory(namespace=_NS, content="body-A")
    anchor_a = {
        **mem.model_dump(mode="json"),
        "point_kind": "anchor",
        "live_point": "cpA",
        "pointer_version": 1,
    }
    vector_a = [0.5] * 1024
    content_point = type(
        "P",
        (),
        {
            "payload": {
                "object_id": str(mem.object_id),
                "namespace": _NS,
                "point_kind": "content",
                "content": "body-A",
            },
            "vector": {DENSE_VECTOR_NAME: vector_a},
        },
    )()

    class _SwapStub:
        """A concurrent pointer swap: any RE-READ of the anchor would return a DIFFERENT object B — so
        a torn resolver that re-reads the anchor would pair B's payload with A's vector."""

        def __init__(self) -> None:
            self.scroll_calls = 0

        def scroll(self, **_kw: Any) -> tuple[list[Any], Any]:
            self.scroll_calls += 1
            return (
                [type("P", (), {"payload": {"object_id": "OBJ-B", "point_kind": "anchor"}})()],
                None,
            )

        def retrieve(self, **_kw: Any) -> list[Any]:
            return [content_point]

    stub = _SwapStub()
    result = _resolve_candidate_memory(stub, _COLL, anchor_a, None)  # type: ignore[arg-type]
    assert result is not None
    assert str(result.memory.object_id) == str(mem.object_id), (
        "must use the caller's single anchor snapshot, not a re-read"
    )
    assert result.vector == vector_a
    assert stub.scroll_calls == 0, "the resolver must NOT re-read the anchor (no torn-read window)"


# ==================================================================================================
# Unit C: episodic operator delete removes the COMPLETE v1/v2 layout across BOTH id spaces.
# ==================================================================================================
def _plane(qdrant: QdrantClient) -> Any:
    from musubi.planes.episodic import EpisodicPlane

    return EpisodicPlane(client=qdrant, embedder=FakeEmbedder())


def _dense(text: str) -> list[float]:
    return asyncio.run(FakeEmbedder().embed_dense([text]))[0]


def _upsert_v1(qdrant: QdrantClient, oid: str, **payload: Any) -> None:
    from musubi.planes.episodic.plane import episodic_point_id

    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=episodic_point_id(oid),
                payload={
                    "object_id": oid,
                    "namespace": _NS,
                    "content": oid,
                    "state": "matured",
                    **payload,
                },
                vector={
                    DENSE_VECTOR_NAME: _dense(oid),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )


def _convert_to_v2(qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, oid: str) -> None:
    """A v1 row that a vector-changing publish converts IN PLACE — the anchor keeps the legacy id."""
    from musubi.store.immutable_vectors import ImmutableVectorPublisher

    _upsert_v1(qdrant, oid)
    pub = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=_COLL)
    pub.register(coord)
    pub.publish(coord, object_id=oid, namespace=_NS, content_payload={"content": oid + "-new-body"})


def _points_for(qdrant: QdrantClient, oid: str) -> list[Any]:
    recs, _ = qdrant.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
        limit=100,
        with_payload=True,
    )
    return list(recs)


def _content_points_for(qdrant: QdrantClient, oid: str) -> list[Any]:
    return [p for p in _points_for(qdrant, oid) if (p.payload or {}).get("point_kind") == "content"]


async def _delete(qdrant: QdrantClient, oid: str, namespace: str = _NS) -> Any:
    return await _plane(qdrant).delete(
        namespace=namespace, object_id=oid, actor="op", reason="test", is_operator=True
    )


def test_delete_removes_v1_layout(qdrant: QdrantClient) -> None:
    _upsert_v1(qdrant, "del-v1")
    asyncio.run(_delete(qdrant, "del-v1"))
    assert _points_for(qdrant, "del-v1") == [], "a v1 row must be fully removed"


def test_delete_removes_converted_v2_layout(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.planes.episodic.plane import episodic_point_id
    from musubi.store.immutable_vectors import read_anchor

    _convert_to_v2(qdrant, coord, "del-cv2")
    anchor = read_anchor(qdrant, _COLL, namespace=_NS, object_id="del-cv2")
    assert anchor is not None  # converted anchor lives at the legacy id
    assert _content_points_for(qdrant, "del-cv2"), "has a content point"
    asyncio.run(_delete(qdrant, "del-cv2"))
    assert _points_for(qdrant, "del-cv2") == [], (
        "converted-v2 anchor (legacy id) + content all removed"
    )
    # sanity: the legacy id space is empty too
    assert qdrant.retrieve(collection_name=_COLL, ids=[episodic_point_id("del-cv2")]) == []


def test_delete_removes_brand_new_v2_layout(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    from musubi.store.immutable_vectors import anchor_point_id, read_anchor

    _make_v2(qdrant, coord, "del-bn2")
    assert read_anchor(qdrant, _COLL, namespace=_NS, object_id="del-bn2") is not None
    asyncio.run(_delete(qdrant, "del-bn2"))
    assert _points_for(qdrant, "del-bn2") == [], (
        "brand-new-v2 anchor (anchor_point_id) + content removed"
    )
    assert qdrant.retrieve(collection_name=_COLL, ids=[anchor_point_id(_NS, "del-bn2")]) == []


def test_delete_removes_all_content_generations(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    import uuid as _uuid

    _make_v2(qdrant, coord, "del-gen")
    # a superseded generation that was never GC'd (still carries the object's identity).
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=str(_uuid.uuid4()),
                payload={"object_id": "del-gen", "namespace": _NS, "point_kind": "content"},
                vector={
                    DENSE_VECTOR_NAME: _dense("old-gen"),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    assert len(_content_points_for(qdrant, "del-gen")) >= 2
    asyncio.run(_delete(qdrant, "del-gen"))
    assert _content_points_for(qdrant, "del-gen") == [], "every content generation must be swept"
    assert _points_for(qdrant, "del-gen") == []


def test_delete_wrong_namespace_refuses_with_zero_deletion(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    _make_v2(qdrant, coord, "del-ns2")
    _upsert_v1(qdrant, "del-ns1")
    for oid in ("del-ns2", "del-ns1"):
        with pytest.raises(LookupError):
            asyncio.run(_delete(qdrant, oid, namespace="eric/OTHER/episodic"))
        assert _points_for(qdrant, oid), f"a wrong-namespace delete must delete nothing ({oid})"


def test_delete_corrupt_identity_payload_still_removable(qdrant: QdrantClient) -> None:
    # a v1 row whose namespace payload is DAMAGE (a string, but not a canonical namespace).
    _upsert_v1(qdrant, "del-corrupt", namespace="garbage")
    event = asyncio.run(_delete(qdrant, "del-corrupt"))
    assert event.to_state == "archived"
    assert _points_for(qdrant, "del-corrupt") == [], (
        "a corrupted-namespace row must still be removable"
    )


def test_delete_not_found_and_retry_truth(qdrant: QdrantClient) -> None:
    with pytest.raises(LookupError):
        asyncio.run(_delete(qdrant, "never-existed"))
    _upsert_v1(qdrant, "del-once")
    asyncio.run(_delete(qdrant, "del-once"))
    # a second delete of the now-gone object is a truthful not-found, never a phantom success.
    with pytest.raises(LookupError):
        asyncio.run(_delete(qdrant, "del-once"))


def test_delete_content_failure_preserves_identity_then_retry_removes_all(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    """Ordering proof (Yua): content is deleted FIRST — if it fails, the identity SURVIVES so a retry
    can still locate and finish. Identity is never deleted first (which would strand the content)."""
    _make_v2(qdrant, coord, "del-fail")

    class _FailContentDeleteOnce:
        def __init__(self, real: Any) -> None:
            self._real = real
            self._failed = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._real, name)

        def delete(self, *, collection_name: str, points_selector: Any, **kw: Any) -> Any:
            if not self._failed and isinstance(points_selector, models.Filter):
                self._failed = True
                raise RuntimeError("injected content-delete failure")
            return self._real.delete(
                collection_name=collection_name, points_selector=points_selector, **kw
            )

    failing = _FailContentDeleteOnce(qdrant)
    with pytest.raises(RuntimeError, match="injected content-delete failure"):
        asyncio.run(
            _plane(cast(QdrantClient, failing)).delete(
                namespace=_NS, object_id="del-fail", actor="op", reason="t", is_operator=True
            )
        )
    # the identity (and its content) SURVIVE the failed content cleanup — still locatable for retry.
    assert _points_for(qdrant, "del-fail"), (
        "a content-delete failure must not strand: identity survives"
    )
    # retry against the healthy client removes everything.
    asyncio.run(_delete(qdrant, "del-fail"))
    assert _points_for(qdrant, "del-fail") == []


# ==================================================================================================
# Unit B: episodic anchor-aware ranked reads (query + dedup). Rank v1/content, never anchors; accept a
# v2 candidate only when it IS the committed live_point; apply state POST-hydration; bounded overfetch.
# ==================================================================================================
import uuid as _uuidmod  # noqa: E402


def _upsert_content(qdrant: QdrantClient, oid: str, text: str, *, cid: str | None = None) -> str:
    """Upsert a STALE (not-live) content point for oid with a vector = embed(text)."""
    point_id = cid or str(_uuidmod.uuid4())
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=point_id,
                payload={
                    "object_id": oid,
                    "namespace": _NS,
                    "point_kind": "content",
                    "content": text,
                },
                vector={
                    DENSE_VECTOR_NAME: _dense(text),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    return point_id


def _upsert_kind(qdrant: QdrantClient, oid: str, kind: str, text: str) -> None:
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=str(_uuidmod.uuid4()),
                payload={
                    "object_id": oid,
                    "namespace": _NS,
                    "point_kind": kind,
                    "content": text,
                    "state": "matured",
                },
                vector={
                    DENSE_VECTOR_NAME: _dense(text),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )


def _full_v1(qdrant: QdrantClient, content: str, **extra: Any) -> str:
    """A v1 row carrying a COMPLETE, model-valid EpisodicMemory payload (so a ranked read exposes it)."""
    from musubi.planes.episodic.plane import episodic_point_id
    from musubi.types.episodic import EpisodicMemory

    mem = EpisodicMemory(namespace=_NS, content=content, **extra)
    oid = str(mem.object_id)
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=episodic_point_id(oid),
                payload=mem.model_dump(mode="json"),
                vector={
                    DENSE_VECTOR_NAME: _dense(content),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    return oid


def _full_v2(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, content: str, **extra: Any
) -> str:
    """A v2 object (anchor + content) whose committed payload is a COMPLETE EpisodicMemory."""
    from musubi.store.immutable_vectors import ImmutableVectorPublisher
    from musubi.types.episodic import EpisodicMemory

    mem = EpisodicMemory(namespace=_NS, content=content, **extra)
    oid = str(mem.object_id)
    pub = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=_COLL)
    pub.register(coord)
    pub.publish(coord, object_id=oid, namespace=_NS, content_payload=mem.model_dump(mode="json"))
    return oid


async def _query(qdrant: QdrantClient, text: str, **kw: Any) -> list[Any]:
    return cast("list[Any]", await _plane(qdrant).query(namespace=_NS, query=text, **kw))


def test_resolve_ranked_candidate_classification() -> None:
    """The ranked-read classifier (pure, no real Qdrant flakiness): anchors + unknown/corrupt kinds are
    NEVER candidates; only ``point_kind`` ABSENT is v1-self; a content point is accepted ONLY when it is
    the committed live_point of its anchor (a stale/superseded snapshot is rejected)."""
    from musubi.store.immutable_vectors import resolve_ranked_candidate

    class _AnchorStub:
        def __init__(self, anchor_payload: dict[str, Any] | None) -> None:
            self._a = anchor_payload

        def scroll(self, **_kw: Any) -> tuple[list[Any], Any]:
            if self._a is None:
                return ([], None)
            return ([type("P", (), {"payload": self._a})()], None)

    anchor = {"live_point": "cp-live", "object_id": "o1", "namespace": _NS}
    stub = cast(QdrantClient, _AnchorStub(anchor))
    content = {"point_kind": "content", "object_id": "o1", "namespace": _NS, "content": "c"}
    # anchor never ranks; unknown kind never ranks (fail closed)
    assert (
        resolve_ranked_candidate(stub, _COLL, point_id="x", payload={"point_kind": "anchor"})
        is None
    )
    assert (
        resolve_ranked_candidate(stub, _COLL, point_id="x", payload={"point_kind": "weird"}) is None
    )
    # v1 (no point_kind) is self-authoritative
    v1 = {"object_id": "o0", "namespace": _NS, "content": "c0"}
    assert resolve_ranked_candidate(stub, _COLL, point_id="x", payload=v1) == v1
    # a content point that IS the live_point hydrates anchor-over-content
    live = resolve_ranked_candidate(stub, _COLL, point_id="cp-live", payload=content)
    assert live is not None and live.get("live_point") == "cp-live"
    # a stale/superseded content snapshot (not the live_point) is rejected
    assert resolve_ranked_candidate(stub, _COLL, point_id="cp-stale", payload=content) is None
    # a content point with NO committed anchor fails closed
    assert (
        resolve_ranked_candidate(
            cast(QdrantClient, _AnchorStub(None)), _COLL, point_id="cp", payload=content
        )
        is None
    )


def test_query_ranks_v1_and_healthy_v2(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    v1 = _full_v1(qdrant, "alpha-body", state="matured")
    v2 = _full_v2(qdrant, coord, "beta-body", state="matured")
    got = {str(m.object_id): m for m in asyncio.run(_query(qdrant, "alpha-body", limit=10))}
    assert v1 in got and v2 in got, "both v1 and healthy v2 rank"
    assert got[v2].content == "beta-body", "the v2 hit is the RESOLVED committed content"


def test_query_rejects_stale_higher_scoring_content(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    v2 = _full_v2(qdrant, coord, "live-body", state="matured")
    _upsert_content(qdrant, v2, "MATCH")  # a STALE content that scores 1.0 for query "MATCH"
    got = [m for m in asyncio.run(_query(qdrant, "MATCH", limit=10)) if str(m.object_id) == v2]
    assert len(got) == 1, (
        "the object appears once — via its live candidate, never the stale snapshot"
    )
    assert got[0].content == "live-body", "the stale higher-scoring content is rejected; live wins"


def test_query_dangling_and_unknown_kind_fail_closed(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    dangle = _full_v2(qdrant, coord, "dangle-body", state="matured")
    _make_dangling(qdrant, dangle)  # committed content deleted
    _upsert_kind(qdrant, "q-unknown", "sasquatch", "q-unknown")  # corrupt/unknown point_kind
    ids = {str(m.object_id) for m in asyncio.run(_query(qdrant, "dangle-body", limit=10))}
    assert dangle not in ids, "a dangling committed pointer fails closed in ranked reads"
    assert "q-unknown" not in ids, "an unknown/corrupt point_kind is never ranked (fail closed)"


def test_query_state_filter_is_post_hydration(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    arch = _full_v2(qdrant, coord, "arch-body", state="archived")  # archived on the anchor
    mat = _full_v2(qdrant, coord, "mat-body", state="matured")
    ids = {str(m.object_id) for m in asyncio.run(_query(qdrant, "mat-body", limit=10))}
    assert mat in ids and arch not in ids, (
        "archived filtered by AUTHORITATIVE anchor state (post-hydration)"
    )


def test_query_malformed_candidate_fails_closed(qdrant: QdrantClient) -> None:
    # State=matured so the state check PASSES and the safe-validate seam is load-bearing; content is
    # omitted (required by EpisodicMemory) so an old direct model_validate would RAISE (Yua).
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=str(_uuidmod.uuid4()),
                payload={"object_id": "q-bad", "namespace": _NS, "state": "matured"},  # no content
                vector={
                    DENSE_VECTOR_NAME: _dense("q-bad"),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    ids = {str(m.object_id) for m in asyncio.run(_query(qdrant, "q-bad", limit=10))}
    assert "q-bad" not in ids, "a malformed (state-passing) candidate is skipped, never a 500"


def test_dedup_walks_past_many_stale_to_the_live_candidate(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    """A destructive duplicate-create is prevented only if the live duplicate is not hidden behind
    stale/superseded higher-scoring content. Bury the live candidate behind 6 stale ones (all scoring
    higher for the probe) and prove the capped budget still finds it (Yua: budget must exceed 4)."""
    from musubi.planes.episodic import EpisodicPlane

    v2 = _full_v2(
        qdrant, coord, "live-dup", state="matured"
    )  # live content vector = embed("live-dup")
    for _ in range(6):
        _upsert_content(qdrant, v2, "PROBE")  # stale, score 1.0 for probe embed("PROBE")
    plane = EpisodicPlane(client=qdrant, embedder=FakeEmbedder(), dedup_threshold=-1.0)
    found = plane._find_dedup_candidate(_NS, _dense("PROBE"))
    assert found is not None, (
        "the live duplicate must be found past >4 stale higher-scoring snapshots"
    )
    assert str(found[0].object_id) == v2
