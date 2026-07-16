"""DATA-001 Phase 2 — curated anchor-aware retrieval (unit B) discriminators (#530).

Curated is the second anchor-aware plane. These prove — against REAL Qdrant — the frozen retrieval rule
on ``CuratedPlane.query`` (rank content + v1, never anchors; resolve through the anchor; state AND the
bitemporal window applied POST-hydration on the TYPED row; malformed candidate skipped, never a 500) and
the three vault seams (``_find_by_vault_path`` #8, ``find_by_vault_path`` #9, ``scan_vault_rows`` #12),
whose fail-closed contract is the INVERSE of the ranked query: a dangling/malformed IDENTITY must raise
(scan / private find) or surface a typed ``invalid_row`` (public find), never collapse to a clean
absence — else ``create`` manufactures a duplicate or the reconciler archives on an incomplete inventory.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid as _uuidmod
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.planes.curated import CuratedPlane
from musubi.planes.curated.plane import FindByVaultPathError, _point_id
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME
from musubi.types.common import Err, Ok, generate_ksuid
from musubi.types.curated import CuratedKnowledge

pytestmark = pytest.mark.integration

_NS = "eric/data001p2cur/curated"
_COLL = collection_for_plane("curated")


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
        client=qdrant, db_path=tmp_path / "cur-coord.db", backoff_base_s=0.01, backoff_max_s=0.01
    )


@pytest.fixture
def plane(qdrant: QdrantClient, coord: LifecycleTransitionCoordinator) -> CuratedPlane:
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,
        register_immutable_vector_dispatch,
    )

    publisher = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=_COLL)
    register_immutable_vector_dispatch(coord, {_COLL: publisher})
    return CuratedPlane(
        client=qdrant, embedder=FakeEmbedder(), coordinator=coord, vector_publisher=publisher
    )


def _dense(text: str) -> list[float]:
    return asyncio.run(FakeEmbedder().embed_dense([text]))[0]


def _make(
    content: str, *, vault_path: str, oid: str | None = None, **extra: Any
) -> CuratedKnowledge:
    return CuratedKnowledge(
        namespace=_NS,
        title=content,
        content=content,
        vault_path=vault_path,
        body_hash=hashlib.sha256(
            content.encode()
        ).hexdigest(),  # differs per body -> triggers update
        object_id=oid or str(generate_ksuid()),
        **extra,
    )


def _v1(plane: CuratedPlane, content: str, *, vault_path: str, **extra: Any) -> str:
    """A v1 curated row (single self-authoritative point) via the fresh-create path."""
    row = _make(content, vault_path=vault_path, **extra)
    asyncio.run(plane.create(row))
    return str(row.object_id)


def _v2(plane: CuratedPlane, first: str, second: str, *, vault_path: str, **extra: Any) -> str:
    """A v2 curated object (anchor + write-once content): create v1, then a same-id body update — the
    curated same-id update path publishes through the immutable-vector seam."""
    oid = str(generate_ksuid())
    asyncio.run(plane.create(_make(first, vault_path=vault_path, oid=oid, **extra)))
    asyncio.run(plane.create(_make(second, vault_path=vault_path, oid=oid, **extra)))
    return oid


def _make_dangling(qdrant: QdrantClient, oid: str) -> None:
    from musubi.store.immutable_vectors import read_anchor

    anchor = read_anchor(qdrant, _COLL, namespace=_NS, object_id=oid)
    assert anchor is not None and anchor.live_point is not None
    qdrant.delete(collection_name=_COLL, points_selector=[anchor.live_point])


def _upsert_content(qdrant: QdrantClient, oid: str, text: str, **payload_extra: Any) -> None:
    """A STALE (non-live) content snapshot for ``oid`` — a valid content point that is NOT the anchor's
    committed live_point, used to prove the resolver rejects a higher-scoring superseded snapshot."""
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=str(_uuidmod.uuid4()),
                payload={
                    "object_id": oid,
                    "namespace": _NS,
                    "point_kind": "content",
                    "content": text,
                    "title": text,
                    **payload_extra,
                },
                vector={
                    DENSE_VECTOR_NAME: _dense(text),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )


def _query(plane: CuratedPlane, text: str, **kw: Any) -> list[CuratedKnowledge]:
    return asyncio.run(plane.query(namespace=_NS, query=text, **kw))


# ==================================================================================================
# query — anchor-aware ranked read
# ==================================================================================================
def test_query_returns_v1_and_healthy_v2(plane: CuratedPlane) -> None:
    v1 = _v1(plane, "alpha curated body", vault_path="a.md")
    v2 = _v2(plane, "beta first body", "beta second body committed", vault_path="b.md")
    got = {str(m.object_id): m for m in _query(plane, "alpha curated body", limit=10)}
    assert v1 in got and v2 in got, "both v1 and healthy v2 rank"
    assert got[v2].content == "beta second body committed", (
        "the v2 hit is the RESOLVED committed content"
    )


def test_query_rejects_stale_higher_scoring_content(
    plane: CuratedPlane, qdrant: QdrantClient
) -> None:
    v2 = _v2(plane, "live one", "live committed body", vault_path="s.md")
    _upsert_content(qdrant, v2, "MATCHWORD")  # a stale snapshot scoring 1.0 for the probe
    got = [m for m in _query(plane, "MATCHWORD", limit=10) if str(m.object_id) == v2]
    assert len(got) == 1, (
        "the object appears once — via its live candidate, never the stale snapshot"
    )
    assert got[0].content == "live committed body", "the stale higher-scoring content is rejected"


def test_query_dangling_v2_fails_closed(plane: CuratedPlane, qdrant: QdrantClient) -> None:
    v2 = _v2(plane, "dangle first", "dangle committed body", vault_path="d.md")
    _make_dangling(qdrant, v2)
    ids = {str(m.object_id) for m in _query(plane, "dangle committed body", limit=10)}
    assert v2 not in ids, "a dangling committed pointer fails closed in the ranked read"


def test_query_bitemporal_window_is_post_hydration(plane: CuratedPlane) -> None:
    """The bitemporal window moved OFF the prefilter (a v2 content point carries no validity — only its
    anchor does) and is applied POST-hydration on the AUTHORITATIVE payload. Uses v2 expired + v2 live so
    the window is proven against the anchor's committed validity, not a legacy v1 self-pointer: the
    expired anchor's live content still matches the probe, yet the object is excluded by its anchor's
    ``valid_until``."""
    from datetime import UTC, datetime, timedelta

    past = datetime.now(UTC) - timedelta(days=1)
    expired = _v2(
        plane, "exp first", "expired body committed", vault_path="exp.md", valid_until=past
    )
    live = _v2(plane, "liv first", "live body committed", vault_path="liv.md")
    ids = {str(m.object_id) for m in _query(plane, "live body committed", limit=10)}
    assert live in ids, "a healthy in-window v2 object ranks"
    assert expired not in ids, "an out-of-window v2 object is filtered post-hydration by its anchor"


def test_query_malformed_validity_is_skipped_not_500(
    plane: CuratedPlane, qdrant: QdrantClient
) -> None:
    """Yua: a candidate that PASSES the state check but carries a malformed ``valid_from_epoch`` (a
    string) must be SKIPPED, never 500 the query. Production validates the typed row FIRST, then compares
    epochs — evaluating the window on the raw payload would ``TypeError`` on the string. State=matured so
    the row survives the state gate and the validity path is genuinely exercised."""
    qdrant.upsert(
        collection_name=_COLL,
        points=[
            models.PointStruct(
                id=_point_id("cur-bad"),
                payload={
                    "object_id": "cur-bad",
                    "namespace": _NS,
                    "state": "matured",
                    "title": "bad validity",
                    "content": "bad validity body",
                    "vault_path": "bad.md",
                    "valid_from_epoch": "not-a-number",  # would TypeError under a raw comparison
                },
                vector={
                    DENSE_VECTOR_NAME: _dense("bad validity body"),
                    "sparse_splade_v1": models.SparseVector(indices=[], values=[]),
                },
            )
        ],
        wait=True,
    )
    ids = {str(m.object_id) for m in _query(plane, "bad validity body", limit=10)}
    assert "cur-bad" not in ids, (
        "a malformed-validity candidate is skipped (fail closed), never a 500"
    )


# ==================================================================================================
# #8 _find_by_vault_path (private) — distinguishes absent (None) from dangling/malformed (raise)
# ==================================================================================================
def test_private_find_by_vault_path_absent_returns_none(plane: CuratedPlane) -> None:
    assert plane._find_by_vault_path(namespace=_NS, vault_path="nope.md") is None


def test_private_find_by_vault_path_resolves_v2_identity(plane: CuratedPlane) -> None:
    v2 = _v2(plane, "one", "two committed", vault_path="p8.md")
    found = plane._find_by_vault_path(namespace=_NS, vault_path="p8.md")
    assert found is not None and str(found.object_id) == v2
    assert found.content == "two committed", "resolves through the anchor to the committed content"


def test_private_find_by_vault_path_dangling_raises(
    plane: CuratedPlane, qdrant: QdrantClient
) -> None:
    v2 = _v2(plane, "one", "two committed", vault_path="p8d.md")
    _make_dangling(qdrant, v2)
    with pytest.raises(ValueError, match="dangling"):
        plane._find_by_vault_path(namespace=_NS, vault_path="p8d.md")


def test_private_find_by_vault_path_content_shell_does_not_shadow(
    plane: CuratedPlane, qdrant: QdrantClient
) -> None:
    """A normal content point carries no ``vault_path``, so ``must_not`` content defends against a
    CORRUPT/future shell that DOES. Inject such a shell (its own fake object_id) at the same path and
    prove the identity scroll still resolves the real ANCHOR — without ``must_not`` content the limit=1
    scroll could return the shell and resolve its (nonexistent) object -> wrong answer or a false raise."""
    v2 = _v2(plane, "one", "two committed", vault_path="p8s.md")
    _upsert_content(qdrant, "corrupt-shell-oid", "shell body", vault_path="p8s.md")
    found = plane._find_by_vault_path(namespace=_NS, vault_path="p8s.md")
    assert found is not None and str(found.object_id) == v2 and found.content == "two committed", (
        "the identity scroll resolves the anchor, never the corrupt content shell at the same path"
    )


# ==================================================================================================
# #9 find_by_vault_path (public) — content shells never inflate; dangling/malformed -> typed invalid_row
# ==================================================================================================
def test_public_find_by_vault_path_v2_single_identity(
    plane: CuratedPlane, qdrant: QdrantClient
) -> None:
    """One healthy v2 object is ONE identity -> Ok. A normal content point carries no ``vault_path`` and
    so cannot inflate the count; to make ``must_not`` content load-bearing here, inject a CORRUPT content
    shell carrying the same ``vault_path`` and prove it is still a single identity (not multiple_matches).
    """
    v2 = _v2(plane, "one", "two committed", vault_path="p9.md")
    _upsert_content(qdrant, "corrupt-shell-oid", "shell body", vault_path="p9.md")
    res = asyncio.run(plane.find_by_vault_path("p9.md"))
    assert isinstance(res, Ok) and str(res.value.object_id) == v2, (
        "a corrupt content shell at the same path must not inflate the identity count"
    )


def test_public_find_by_vault_path_two_identities_multiple_matches(plane: CuratedPlane) -> None:
    """The frozen VAULT-003 fail-closed: two DISTINCT identities sharing one ``vault_path`` (the
    uniqueness invariant violated) must surface as ``multiple_matches`` so the watcher refuses to archive
    an arbitrary one — proven against real Qdrant with two independent v1 objects."""
    _v1(plane, "first identity", vault_path="dupe.md")
    _v1(plane, "second identity", vault_path="dupe.md")
    res = asyncio.run(plane.find_by_vault_path("dupe.md"))
    assert isinstance(res, Err)
    assert isinstance(res.error, FindByVaultPathError) and res.error.code == "multiple_matches", (
        "two distinct identities at one vault_path fail closed as multiple_matches"
    )
    assert res.error.match_count >= 2


def test_public_find_by_vault_path_dangling_is_invalid_row(
    plane: CuratedPlane, qdrant: QdrantClient
) -> None:
    v2 = _v2(plane, "one", "two committed", vault_path="p9d.md")
    _make_dangling(qdrant, v2)
    res = asyncio.run(plane.find_by_vault_path("p9d.md"))
    assert isinstance(res, Err)
    assert isinstance(res.error, FindByVaultPathError) and res.error.code == "invalid_row", (
        "a present-but-dangling identity must surface as invalid_row, never a clean not_found"
    )


def test_public_find_by_vault_path_absent_is_not_found(plane: CuratedPlane) -> None:
    res = asyncio.run(plane.find_by_vault_path("truly-absent.md"))
    assert isinstance(res, Err) and res.error.code == "not_found"


# ==================================================================================================
# #12 scan_vault_rows — excludes content shells; fail-loud (raise) on dangling/malformed
# ==================================================================================================
def test_scan_counts_v2_object_once_excluding_content(plane: CuratedPlane) -> None:
    _v1(plane, "scan v1", vault_path="sc1.md")
    _v2(plane, "one", "scan v2 committed", vault_path="sc2.md")
    rows = asyncio.run(plane.scan_vault_rows())
    paths = [r.vault_path for r in rows if r.namespace == _NS]
    assert sorted(paths) == ["sc1.md", "sc2.md"], (
        "each object appears ONCE — content shells (which carry vault_path too) are excluded"
    )


def test_scan_dangling_identity_raises(plane: CuratedPlane, qdrant: QdrantClient) -> None:
    v2 = _v2(plane, "one", "two committed", vault_path="scd.md")
    _make_dangling(qdrant, v2)
    with pytest.raises(ValueError, match="dangling"):
        asyncio.run(plane.scan_vault_rows())
