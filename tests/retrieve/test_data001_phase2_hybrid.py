"""DATA-001 Phase 2 — anchor-aware HYBRID retrieval (unit B) discriminators (#530).

Hybrid is the last anchor-aware read seam. Against REAL Qdrant these prove the frozen retrieval rule on
``hybrid_search`` for the two immutable-vector planes (rank content + v1, never anchors on EITHER fusion
leg; resolve through the anchor; validate into the plane model, skipping a malformed row; state and
(curated) the bitemporal window applied POST-hydration on the validated row; candidate RRF score
preserved; bounded overfetch) AND that concept/thought/artifact stay byte-for-byte on the pre-P2 raw path
(no resolver round-trip) — the multi-plane parity requirement.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid as _uuidmod
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.retrieve.hybrid import hybrid_search
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.common import Ok, generate_ksuid

pytestmark = pytest.mark.integration

_NS_EP = "eric/data001p2hy/episodic"
_NS_CUR = "eric/data001p2hy/curated"
_EP = collection_for_plane("episodic")
_CUR = collection_for_plane("curated")


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    port = os.environ.get("MUSUBI_TEST_QDRANT_PORT")
    client = QdrantClient(host="localhost", port=int(port)) if port else QdrantClient(":memory:")
    bootstrap(client)

    def _wipe() -> None:
        for coll, ns in ((_EP, _NS_EP), (_CUR, _NS_CUR)):
            client.delete(
                collection_name=coll,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="namespace", match=models.MatchValue(value=ns)
                            )
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
        client=qdrant, db_path=tmp_path / "hy-coord.db", backoff_base_s=0.01, backoff_max_s=0.01
    )


def _dense(text: str) -> list[float]:
    return asyncio.run(FakeEmbedder().embed_dense([text]))[0]


def _sparse(text: str) -> dict[int, float]:
    return asyncio.run(FakeEmbedder().embed_sparse([text]))[0]


def _publisher(qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, collection: str) -> Any:
    from musubi.store.immutable_vectors import ImmutableVectorPublisher

    pub = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=collection)
    pub.register(coord)
    return pub


def _ep_v1(qdrant: QdrantClient, content: str, **extra: Any) -> str:
    from musubi.planes.episodic.plane import episodic_point_id
    from musubi.types.episodic import EpisodicMemory

    mem = EpisodicMemory(namespace=_NS_EP, content=content, **extra)
    oid = str(mem.object_id)
    qdrant.upsert(
        collection_name=_EP,
        points=[
            models.PointStruct(
                id=episodic_point_id(oid),
                payload=mem.model_dump(mode="json"),
                vector={
                    DENSE_VECTOR_NAME: _dense(content),
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(_sparse(content).keys()),
                        values=list(_sparse(content).values()),
                    ),
                },
            )
        ],
        wait=True,
    )
    return oid


def _ep_v2(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, content: str, **extra: Any
) -> str:
    from musubi.types.episodic import EpisodicMemory

    mem = EpisodicMemory(namespace=_NS_EP, content=content, **extra)
    oid = str(mem.object_id)
    _publisher(qdrant, coord, _EP).publish(
        coord, object_id=oid, namespace=_NS_EP, content_payload=mem.model_dump(mode="json")
    )
    return oid


def _cur_v2(
    qdrant: QdrantClient,
    coord: LifecycleTransitionCoordinator,
    first: str,
    second: str,
    *,
    vault_path: str,
    **extra: Any,
) -> str:
    from musubi.planes.curated import CuratedPlane
    from musubi.store.immutable_vectors import register_immutable_vector_dispatch
    from musubi.types.curated import CuratedKnowledge

    pub = _publisher(qdrant, coord, _CUR)
    register_immutable_vector_dispatch(coord, {_CUR: pub})
    plane = CuratedPlane(
        client=qdrant, embedder=FakeEmbedder(), coordinator=coord, vector_publisher=pub
    )
    oid = str(generate_ksuid())
    for body in (
        first,
        second,
    ):  # same-id UPDATE (distinct vault_path per object -> no supersession)
        row = CuratedKnowledge(
            namespace=_NS_CUR,
            title=body,
            content=body,
            vault_path=vault_path,
            body_hash=hashlib.sha256(body.encode()).hexdigest(),
            object_id=oid,
            **extra,
        )
        asyncio.run(plane.create(row))
    return oid


def _cur_v1(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator, content: str, *, vault_path: str
) -> str:
    """A v1 curated row (single self-authoritative point) via the fresh-create path."""
    from musubi.planes.curated import CuratedPlane
    from musubi.store.immutable_vectors import register_immutable_vector_dispatch
    from musubi.types.curated import CuratedKnowledge

    pub = _publisher(qdrant, coord, _CUR)
    register_immutable_vector_dispatch(coord, {_CUR: pub})
    plane = CuratedPlane(
        client=qdrant, embedder=FakeEmbedder(), coordinator=coord, vector_publisher=pub
    )
    row = CuratedKnowledge(
        namespace=_NS_CUR,
        title=content,
        content=content,
        vault_path=vault_path,
        body_hash=hashlib.sha256(content.encode()).hexdigest(),
        object_id=str(generate_ksuid()),
    )
    asyncio.run(plane.create(row))
    return str(row.object_id)


def _make_dangling(qdrant: QdrantClient, collection: str, ns: str, oid: str) -> None:
    from musubi.store.immutable_vectors import read_anchor

    anchor = read_anchor(qdrant, collection, namespace=ns, object_id=oid)
    assert anchor is not None and anchor.live_point is not None
    qdrant.delete(collection_name=collection, points_selector=[anchor.live_point])


def _hits(qdrant: QdrantClient, collection: str, ns: str, query: str, **kw: Any) -> list[Any]:
    res = asyncio.run(
        hybrid_search(
            qdrant, FakeEmbedder(), namespace=ns, query=query, collection=collection, **kw
        )
    )
    assert isinstance(res, Ok), f"expected Ok, got {res!r}"
    return res.value.hits


# ==================================================================================================
# episodic hybrid — anchor-aware
# ==================================================================================================
def test_hybrid_ranks_v1_and_healthy_v2(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    v1 = _ep_v1(qdrant, "alpha hybrid body", state="matured")
    v2 = _ep_v2(qdrant, coord, "beta hybrid committed", state="matured")
    hits = {h.object_id: h for h in _hits(qdrant, _EP, _NS_EP, "alpha hybrid body", limit=10)}
    assert v1 in hits and v2 in hits, "both v1 and healthy v2 rank in hybrid fusion"
    assert hits[v2].payload.get("content") == "beta hybrid committed", (
        "v2 hit is the committed content"
    )
    assert "point_kind" not in hits[v2].payload, "layout keys are stripped from the hit payload"


def test_hybrid_rejects_stale_higher_scoring_content(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    v2 = _ep_v2(qdrant, coord, "live hybrid body", state="matured")
    qdrant.upsert(  # a stale (non-live) content snapshot scoring high for the probe
        collection_name=_EP,
        points=[
            models.PointStruct(
                id=str(_uuidmod.uuid4()),
                payload={
                    "object_id": v2,
                    "namespace": _NS_EP,
                    "point_kind": "content",
                    "content": "MATCHWORD",
                },
                vector={
                    DENSE_VECTOR_NAME: _dense("MATCHWORD"),
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(_sparse("MATCHWORD").keys()),
                        values=list(_sparse("MATCHWORD").values()),
                    ),
                },
            )
        ],
        wait=True,
    )
    hits = [h for h in _hits(qdrant, _EP, _NS_EP, "MATCHWORD", limit=10) if h.object_id == v2]
    assert len(hits) == 1 and hits[0].payload.get("content") == "live hybrid body", (
        "the stale higher-scoring snapshot is rejected; the live committed content wins"
    )


def test_hybrid_anchor_never_ranks_on_either_leg(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    """Force the anchor's dense AND sparse vectors to the exact query (= the live content's text) so it
    would top BOTH fusion legs if it were not excluded. The object MUST still be present (via its live
    content — so empty output cannot vacuously pass), and NO hit may be anchor-derived."""
    from musubi.store.immutable_vectors import anchor_point_id

    probe = "anchor probe committed body"
    v2 = _ep_v2(qdrant, coord, probe, state="matured")
    qdrant.update_vectors(
        collection_name=_EP,
        points=[
            models.PointVectors(
                id=anchor_point_id(_NS_EP, v2),
                vector={
                    DENSE_VECTOR_NAME: _dense(
                        probe
                    ),  # anchor forced to the exact query on both legs
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(_sparse(probe).keys()), values=list(_sparse(probe).values())
                    ),
                },
            )
        ],
    )
    hits = _hits(qdrant, _EP, _NS_EP, probe, limit=10)
    by_id = {h.object_id: h for h in hits}
    assert v2 in by_id, (
        "the object is present via its live content (guards against vacuous empty-pass)"
    )
    assert by_id[v2].payload.get("content") == probe, (
        "present via committed content, not the anchor"
    )
    for h in hits:
        assert h.payload.get("point_kind") != "anchor", "an anchor must never appear as a hit"
        assert "live_point" not in h.payload, "no anchor-only shell leaks into a hit"


def test_hybrid_dangling_fails_closed(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    v2 = _ep_v2(qdrant, coord, "dangle hybrid committed", state="matured")
    _make_dangling(qdrant, _EP, _NS_EP, v2)
    ids = {h.object_id for h in _hits(qdrant, _EP, _NS_EP, "dangle hybrid committed", limit=10)}
    assert v2 not in ids, "a dangling committed pointer fails closed in hybrid"


def test_hybrid_state_filter_is_post_hydration(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    arch = _ep_v2(qdrant, coord, "arch hybrid committed", state="archived")
    mat = _ep_v2(qdrant, coord, "mat hybrid committed", state="matured")
    ids = {h.object_id for h in _hits(qdrant, _EP, _NS_EP, "mat hybrid committed", limit=10)}
    assert mat in ids and arch not in ids, (
        "archived excluded by authoritative anchor state post-hydration"
    )


def test_hybrid_malformed_authoritative_is_skipped(qdrant: QdrantClient) -> None:
    """A v1-shape row that PASSES the state gate but is missing a required model field must be skipped
    (fail closed), never escape as a hit that 500s a downstream re-validation."""
    from musubi.planes.episodic.plane import episodic_point_id

    qdrant.upsert(
        collection_name=_EP,
        points=[
            models.PointStruct(
                id=episodic_point_id("ep-bad"),
                payload={
                    "object_id": "ep-bad",
                    "namespace": _NS_EP,
                    "state": "matured",
                },  # no content
                vector={
                    DENSE_VECTOR_NAME: _dense("bad body"),
                    SPARSE_VECTOR_NAME: models.SparseVector(
                        indices=list(_sparse("bad body").keys()),
                        values=list(_sparse("bad body").values()),
                    ),
                },
            )
        ],
        wait=True,
    )
    ids = {h.object_id for h in _hits(qdrant, _EP, _NS_EP, "bad body", limit=10)}
    assert "ep-bad" not in ids, "a malformed authoritative row is skipped, never a hit"


# ==================================================================================================
# curated hybrid — bitemporal post-hydration
# ==================================================================================================
def test_hybrid_curated_expired_v2_excluded_live_included(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    expired = _cur_v2(
        qdrant, coord, "exp one", "expired hybrid committed", vault_path="exp.md", valid_until=past
    )
    live = _cur_v2(qdrant, coord, "liv one", "live hybrid committed", vault_path="liv.md")
    ids = {h.object_id for h in _hits(qdrant, _CUR, _NS_CUR, "live hybrid committed", limit=10)}
    assert live in ids, "a healthy in-window curated v2 ranks in hybrid"
    assert expired not in ids, (
        "an out-of-window curated v2 is filtered post-hydration by its anchor"
    )


def test_hybrid_curated_ranks_v1_and_healthy_v2(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    """Frozen matrix: v1 + healthy v2 rank on BOTH immutable planes — the curated counterpart to the
    episodic mixed-rank proof."""
    v1 = _cur_v1(qdrant, coord, "curated alpha body", vault_path="cv1.md")
    v2 = _cur_v2(qdrant, coord, "beta one", "curated beta committed", vault_path="cv2.md")
    ids = {h.object_id for h in _hits(qdrant, _CUR, _NS_CUR, "curated alpha body", limit=10)}
    assert v1 in ids and v2 in ids, "both a v1 curated row and a healthy v2 curated object rank"


# ==================================================================================================
# multi-plane parity — concept/thought/artifact stay on the raw pre-P2 path (no resolver round-trip)
# ==================================================================================================
@pytest.mark.parametrize("plane_name", ["concept", "thought", "artifact"])
def test_hybrid_non_anchor_plane_takes_raw_path_no_resolver(
    plane_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The frozen parity contract names all three non-anchor planes. This drives PRODUCTION
    ``hybrid_search`` end-to-end so the COLLECTION GATE itself is under test (a helper-level call could
    not catch the gate wrongly routing concept/thought/artifact into anchor-aware mode). ``resolve_ranked_
    candidate`` is monkeypatched to fail if ever called; the raw fused hit must be returned BYTE-UNCHANGED
    and the resolver must be reached ZERO times."""
    from typing import cast

    import musubi.retrieve.hybrid as hy

    calls = {"n": 0}

    def _must_not_resolve(*_a: Any, **_k: Any) -> Any:
        calls["n"] += 1
        raise AssertionError("a non-anchor collection must not reach the resolver (raw path)")

    monkeypatch.setattr(hy, "resolve_ranked_candidate", _must_not_resolve)

    raw_payload = {"object_id": "o1", "state": "matured", "content": "c", "point_kind": "content"}

    class _Pt:
        def __init__(self, pid: str, score: float, payload: dict[str, Any]) -> None:
            self.id, self.score, self.payload = pid, score, payload

    class _SpyClient:
        def query_points(self, *_a: Any, **_k: Any) -> Any:
            return type("R", (), {"points": [_Pt("p1", 0.9, dict(raw_payload))]})()

    res = asyncio.run(
        hy.hybrid_search(
            cast(QdrantClient, _SpyClient()),
            FakeEmbedder(),
            namespace=_NS_EP,
            query="anything",
            collection=collection_for_plane(plane_name),
            limit=10,
        )
    )
    assert isinstance(res, Ok)
    assert calls["n"] == 0, "the gate kept a non-anchor collection off the resolver entirely"
    assert [h.object_id for h in res.value.hits] == ["o1"]
    assert res.value.hits[0].payload == raw_payload, (
        "a non-anchor plane returns the raw payload BYTE-UNCHANGED (no strip, no resolve)"
    )


def test_hybrid_gated_filters_carry_must_not_anchor_non_gated_do_not() -> None:
    """Direct filter-shape proof: for a gated collection BOTH fusion-leg prefetch filters AND the
    top-level query filter carry ``must_not`` anchor (and state leaves the top-level filter); for a
    non-anchor collection none of them do and state stays on the top-level filter."""
    from musubi.retrieve.hybrid import (
        _build_filter,
        _build_prefetch,
        _namespace_filter,
        _QueryEmbedding,
    )
    from musubi.store.specs import POINT_KIND_ANCHOR

    def _has_anchor_exclusion(f: models.Filter) -> bool:
        return any(
            isinstance(c, models.FieldCondition)
            and c.key == "point_kind"
            and isinstance(c.match, models.MatchValue)
            and c.match.value == POINT_KIND_ANCHOR
            for c in (f.must_not or [])
        )

    def _has_state(f: models.Filter) -> bool:
        conds = f.must if isinstance(f.must, list) else [f.must] if f.must else []
        return any(isinstance(c, models.FieldCondition) and c.key == "state" for c in conds)

    emb = _QueryEmbedding(dense=[0.1] * 8, sparse={1: 0.5})
    for anchor_aware in (True, False):
        prefetch = _build_prefetch(
            emb,
            limit=10,
            dense_enabled=True,
            sparse_enabled=True,
            namespace_filter=_namespace_filter(_NS_EP),
            anchor_aware=anchor_aware,
        )
        top = _build_filter(
            namespace=_NS_EP, state_filter=None, include_archived=False, anchor_aware=anchor_aware
        )
        assert len(prefetch) == 2, "both dense + sparse legs present"
        for leg in prefetch:
            assert isinstance(leg.filter, models.Filter)
            assert _has_anchor_exclusion(leg.filter) is anchor_aware, (
                "each fusion leg excludes anchors iff gated"
            )
        assert _has_anchor_exclusion(top) is anchor_aware, "top-level excludes anchors iff gated"
        assert _has_state(top) is (not anchor_aware), (
            "state stays on the top-level filter ONLY for non-anchor planes"
        )


def test_hybrid_bounded_underfill_reaches_live_past_higher_stale(
    qdrant: QdrantClient, coord: LifecycleTransitionCoordinator
) -> None:
    """limit=1 with several stale higher-scoring content candidates ahead of the one live lower
    candidate: the bounded OVERFETCH still returns the LIVE row. This FAILS an un-overfetched anchor-aware
    limit=1 shape (which would fetch only the top stale candidate, resolve it to nothing, and return
    empty) — NOT the pre-P2 raw path, which would have returned the stale snapshot as a raw hit."""
    live = _ep_v2(qdrant, coord, "underfill live body", state="matured")
    for _ in range(3):  # > limit stale snapshots that outscore the probe
        qdrant.upsert(
            collection_name=_EP,
            points=[
                models.PointStruct(
                    id=str(_uuidmod.uuid4()),
                    payload={
                        "object_id": live,
                        "namespace": _NS_EP,
                        "point_kind": "content",
                        "content": "PROBEWORD",
                    },
                    vector={
                        DENSE_VECTOR_NAME: _dense("PROBEWORD"),
                        SPARSE_VECTOR_NAME: models.SparseVector(
                            indices=list(_sparse("PROBEWORD").keys()),
                            values=list(_sparse("PROBEWORD").values()),
                        ),
                    },
                )
            ],
            wait=True,
        )
    hits = _hits(qdrant, _EP, _NS_EP, "PROBEWORD", limit=1)
    assert [h.object_id for h in hits] == [live], (
        "the bounded overfetch reaches the live row past stale"
    )
    assert hits[0].payload.get("content") == "underfill live body", (
        "and it is the committed content"
    )
