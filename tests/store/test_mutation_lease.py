"""DATA-001 / #530 — attributable owner-token mutation lease invariants (real Qdrant).

Drives :func:`musubi.store.mutation_lease.owned_update` directly. Bring the server up on port 6339
(``make test-integration-up``). The exhaustion + seam-field guards are unit tests.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic.plane import EpisodicPlane, episodic_point_id
from musubi.store import bootstrap
from musubi.store.mutation_lease import (
    MutationLeaseConflict,
    MutationPlan,
    owned_update,
)
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

_COLL = collection_for_plane("episodic")


@pytest.fixture
def real_qdrant() -> Iterator[QdrantClient]:
    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


def _seed(client: QdrantClient, *, importance: int = 5) -> tuple[str, str]:
    ns = f"ml-{generate_ksuid()[:8].lower()}/dev/episodic"
    row = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(
                namespace=ns, content="mutation lease", state="matured", importance=importance
            )
        )
    )
    return ns, row.object_id


def _row(client: QdrantClient, oid: str, *, with_vectors: bool = False) -> Any:
    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
        limit=1,
        with_payload=True,
        with_vectors=with_vectors,
    )
    return recs[0] if recs else None


def _payload(client: QdrantClient, oid: str) -> dict[str, Any]:
    rec = _row(client, oid)
    return dict(rec.payload or {}) if rec else {}


def _dense(client: QdrantClient, oid: str) -> list[float]:
    rec = _row(client, oid, with_vectors=True)
    vecs = rec.vector if rec else {}
    return cast("list[float]", vecs.get(DENSE_VECTOR_NAME)) if isinstance(vecs, dict) else []


def _set_token(client: QdrantClient, oid: str, token: str) -> None:
    client.set_payload(
        collection_name=_COLL,
        payload={"update_lease_token": token},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )


@pytest.mark.integration
def test_owned_update_publishes_narrow_change_and_bumps_version(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant, importance=5)
    published = owned_update(
        real_qdrant,
        _COLL,
        namespace=ns,
        object_id=oid,
        point_id=episodic_point_id(oid),
        plan=lambda cur: MutationPlan(changes={"tags": ["x"]}),
    )
    assert published["tags"] == ["x"]
    assert published["version"] == 2  # bumped from 1
    assert published["importance"] == 5  # untouched
    assert published.get("update_lease_token") is None  # released


@pytest.mark.integration
def test_unrelated_concurrent_field_composes(real_qdrant: QdrantClient) -> None:
    """The DATA-001 invariant at the seam: a narrow owned_update writes ONLY its intended field, so
    an unrelated field set by another writer is never in the write set and survives."""
    ns, oid = _seed(real_qdrant, importance=5)
    _set_token  # (kept for symmetry with other tests)
    # An unrelated writer changed importance out from under us.
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"importance": 9},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )
    published = owned_update(
        real_qdrant,
        _COLL,
        namespace=ns,
        object_id=oid,
        point_id=episodic_point_id(oid),
        plan=lambda cur: MutationPlan(changes={"tags": ["y"]}),
    )
    assert published["tags"] == ["y"]
    assert published["importance"] == 9  # the unrelated concurrent mutation composed


@pytest.mark.integration
def test_two_writers_same_next_version_both_land_attributably(real_qdrant: QdrantClient) -> None:
    """Yua's discriminator: two contenders both start from version 1 and propose version 2. Exactly
    one wins each version step (attributable via the exact owner token); the loser retries against
    the fresh state and lands at the next version. Both distinct changes survive — no lost update,
    and version==expected+1 alone is NOT treated as a win."""
    ns, oid = _seed(real_qdrant, importance=5)
    barrier = threading.Barrier(2)

    def writer(field: str, value: Any) -> None:
        barrier.wait()
        owned_update(
            real_qdrant,
            _COLL,
            namespace=ns,
            object_id=oid,
            point_id=episodic_point_id(oid),
            plan=lambda cur: MutationPlan(changes={field: value}),
        )

    a = threading.Thread(target=writer, args=("tags", ["from-a"]))
    b = threading.Thread(target=writer, args=("importance", 8))
    a.start()
    b.start()
    a.join()
    b.join()

    row = _payload(real_qdrant, oid)
    assert row["tags"] == ["from-a"]  # writer A's change survived
    assert row["importance"] == 8  # writer B's change survived
    assert row["version"] == 3  # two serialized version steps, no collision
    assert row.get("update_lease_token") is None


@pytest.mark.integration
def test_loser_cannot_change_vector(real_qdrant: QdrantClient) -> None:
    """Yua's requirement: update_vectors is unfenced, so a writer that does NOT win the owner token
    must never reach it. A live foreign owner holds the row; a contender whose plan WOULD change the
    vector fails to acquire, exhausts fail-loud, and the stored vector is untouched."""
    ns, oid = _seed(real_qdrant)
    original = _dense(real_qdrant, oid)
    assert original and len(original) == 1024
    live_foreign = (
        f"own:{int(time.time() * 1_000_000)}:foreignowner"  # issued now → not takeover-able
    )
    _set_token(real_qdrant, oid, live_foreign)

    different = [0.5] * 1024
    with pytest.raises(MutationLeaseConflict):
        owned_update(
            real_qdrant,
            _COLL,
            namespace=ns,
            object_id=oid,
            point_id=episodic_point_id(oid),
            plan=lambda cur: MutationPlan(
                changes={"tags": ["z"]}, vectors={DENSE_VECTOR_NAME: different}
            ),
        )
    assert _dense(real_qdrant, oid) == original  # loser never touched the vector
    assert (
        _payload(real_qdrant, oid).get("update_lease_token") == live_foreign
    )  # foreign lease intact


@pytest.mark.integration
def test_expired_owner_token_takeover_recovers(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant, importance=5)
    expired = f"own:{int(time.time() * 1_000_000) - 10_000_000}:crashedowner"  # 10s ago → expired
    _set_token(real_qdrant, oid, expired)
    published = owned_update(
        real_qdrant,
        _COLL,
        namespace=ns,
        object_id=oid,
        point_id=episodic_point_id(oid),
        plan=lambda cur: MutationPlan(changes={"tags": ["recovered"]}),
    )
    assert published["tags"] == ["recovered"]
    assert published["version"] == 2
    assert published.get("update_lease_token") is None


@pytest.mark.integration
def test_skip_plan_is_noop_and_releases(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant, importance=5)
    published = owned_update(
        real_qdrant,
        _COLL,
        namespace=ns,
        object_id=oid,
        point_id=episodic_point_id(oid),
        plan=lambda cur: MutationPlan(changes={}, skip=True),
    )
    assert published["version"] == 1  # not bumped
    assert published["importance"] == 5
    assert published.get("update_lease_token") is None  # released even on a no-op


@pytest.mark.integration
def test_seam_owned_field_in_changes_is_rejected(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant)
    with pytest.raises(ValueError, match="seam-owned"):
        owned_update(
            real_qdrant,
            _COLL,
            namespace=ns,
            object_id=oid,
            point_id=episodic_point_id(oid),
            plan=lambda cur: MutationPlan(changes={"version": 99}),
        )


class _AlwaysLiveOwnerClient:
    """Fake client whose row always shows a FRESH foreign owner token → acquire can never win."""

    def scroll(self, *_a: Any, **_k: Any) -> Any:
        token = f"own:{int(time.time() * 1_000_000)}:foreign"
        rec = type(
            "R",
            (),
            {
                "id": "p1",
                "payload": {
                    "namespace": "n/n/episodic",
                    "object_id": "o",
                    "version": 1,
                    "update_lease_token": token,
                },
            },
        )()
        return ([rec], None)

    def set_payload(self, *_a: Any, **_k: Any) -> Any:
        return None


def test_exhaustion_is_fail_loud() -> None:
    with pytest.raises(MutationLeaseConflict):
        owned_update(
            cast(Any, _AlwaysLiveOwnerClient()),
            _COLL,
            namespace="n/n/episodic",
            object_id="o",
            point_id="p1",
            plan=lambda cur: MutationPlan(changes={"tags": ["x"]}),
        )
