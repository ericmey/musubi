"""DATA-001 / #530 — cross-mutation lost update on full-object UPDATE paths.

RET-008 made access-writer-vs-access-writer concurrency safe (the fenced lease) and preserved the
lease-owned fields across full-point upserts. It did NOT close the broader race: a full-object
UPDATE (dedup-merge reinforce, curated same-id update, patch/supersede/concept-update merges) reads
a whole object and later writes it back, carrying the read-time snapshot of EVERY non-lease field.
An unrelated concurrent mutation that lands in the read-to-upsert window is silently overwritten.

These proofs model the read-to-upsert window deterministically by pinning the earlier business read
to a pre-mutation snapshot — the same technique used for the RET-008 stale-probe proofs, which Yua
accepted. Bring the server up on port 6339 (``make test-integration-up``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.access_lease import lease_increment_access
from musubi.store.names import collection_for_plane
from musubi.types.common import generate_ksuid
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory

_COLL = collection_for_plane("episodic")
_CURATED_COLL = collection_for_plane("curated")


def _wired_episodic_plane(qdrant: QdrantClient, db_path: Any) -> EpisodicPlane:
    """A fully-wired episodic plane (DATA-001 P2): temp coordinator + collection-bound publisher +
    dispatcher, so a vector-changing reinforce traverses the REAL descriptor/pointer seam (never mocked
    away, never fail-closed). Mirrors the test_episodic.py plane fixture."""
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,
        register_immutable_vector_dispatch,
    )

    coord = LifecycleTransitionCoordinator(
        client=qdrant, db_path=db_path, backoff_base_s=0.01, backoff_max_s=0.01
    )
    pub = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=_COLL)
    register_immutable_vector_dispatch(coord, {_COLL: pub})
    return EpisodicPlane(
        client=qdrant, embedder=FakeEmbedder(), coordinator=coord, vector_publisher=pub
    )


def _wired_curated_plane(qdrant: QdrantClient, db_path: Any) -> CuratedPlane:
    """A fully-wired curated plane (DATA-001 P2) — same seam as above, curated collection."""
    from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
    from musubi.store.immutable_vectors import (
        ImmutableVectorPublisher,
        register_immutable_vector_dispatch,
    )

    coord = LifecycleTransitionCoordinator(
        client=qdrant, db_path=db_path, backoff_base_s=0.01, backoff_max_s=0.01
    )
    pub = ImmutableVectorPublisher(client=qdrant, embedder=FakeEmbedder(), collection=_CURATED_COLL)
    register_immutable_vector_dispatch(coord, {_CURATED_COLL: pub})
    return CuratedPlane(
        client=qdrant, embedder=FakeEmbedder(), coordinator=coord, vector_publisher=pub
    )


@pytest.fixture
def real_qdrant() -> Iterator[QdrantClient]:
    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


def _seed(client: QdrantClient) -> tuple[str, str]:
    ns = f"data001-{generate_ksuid()[:8].lower()}/dev/episodic"
    row = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="occ probe", state="matured", importance=5)
        )
    )
    return ns, row.object_id


def _row(client: QdrantClient, oid: str) -> dict[str, Any]:
    # DATA-001 P2: after a vector-changing reinforce the object is a v2 layout (anchor + content points).
    # The RET-008 access fields + reinforcement_count live on the ANCHOR identity row, not the content
    # shell — exclude content so this returns the authoritative identity (a no-op for a v1 row).
    from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD

    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))],
            must_not=[
                models.FieldCondition(
                    key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                )
            ],
        ),
        limit=1,
        with_payload=True,
    )
    return dict(recs[0].payload or {}) if recs else {}


@pytest.mark.integration
def test_dedup_merge_upsert_loses_concurrent_unrelated_field_update(
    real_qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """DATA-001 RED: the episodic dedup-merge full-point upsert carries the probe-time snapshot of
    every non-lease field. An unrelated concurrent mutation (``importance`` 5 → 9) that lands in the
    read-to-upsert window is silently overwritten. GREEN once the UPDATE writes only the fields it
    intends to change (or fences on version). DATA-001 P2: the reinforce is a vector change, so it
    traverses the REAL immutable-vector seam (publisher wired), and we ASSERT a real reinforce ran
    (reinforcement_count bumps) — a factually-incompatible probe would silently insert a fresh row and
    exercise nothing (Yua)."""
    plane = _wired_episodic_plane(real_qdrant, tmp_path / "occ-unrelated.db")
    ns, oid = _seed(real_qdrant)  # importance == 5; content "occ probe"
    stale = EpisodicMemory.model_validate(_row(real_qdrant, oid))  # snapshot: importance == 5
    rc_before = int(_row(real_qdrant, oid).get("reinforcement_count", 0))

    # A concurrent, UNRELATED mutation lands AFTER the probe read.
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"importance": 9},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )
    assert _row(real_qdrant, oid).get("importance") == 9

    # The dedup-merge upsert fires from the STALE snapshot (importance == 5).
    monkeypatch.setattr(
        plane,
        "_find_dedup_candidate",
        lambda namespace, dense: (
            stale,
            None,
            None,
            1.0,
        ),  # DATA-001 P2 + ING-002: 4-tuple w/ score
    )
    # Compatible surface form of the seed's "occ probe" so a REAL reinforce runs (not a fresh insert).
    asyncio.run(plane.create(EpisodicMemory(namespace=ns, content="Occ probe.", state="matured")))

    row = _row(real_qdrant, oid)
    assert int(row.get("reinforcement_count", 0)) == rc_before + 1, (
        "a real reinforce must have run (else the unrelated-field assertion is vacuous)"
    )
    # DATA-001 invariant: the unrelated concurrent update must survive the reinforce.
    assert row.get("importance") == 9, (
        "reinforce upsert overwrote a concurrent unrelated field (cross-mutation lost update)"
    )


@pytest.mark.integration
def test_reinforce_composes_concurrent_access_increment(
    real_qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The two P0 fixes compose: a leased access_count increment (RET-008) is preserved by a dedup
    reinforce (DATA-001) — the reinforce's narrow change-set never touches the access fields — and the
    reinforce still lands (reinforcement_count bumps)."""
    plane = _wired_episodic_plane(real_qdrant, tmp_path / "occ-reinforce.db")
    ns, oid = _seed(real_qdrant)
    asyncio.run(lease_increment_access(real_qdrant, _COLL, {(ns, oid)}))  # access_count -> 1
    assert _row(real_qdrant, oid).get("access_count") == 1
    rc_before = int(_row(real_qdrant, oid).get("reinforcement_count", 0))

    stale = EpisodicMemory.model_validate(_row(real_qdrant, oid))
    monkeypatch.setattr(
        plane,
        "_find_dedup_candidate",
        lambda namespace, dense: (
            stale,
            None,
            None,
            1.0,
        ),  # DATA-001 P2 + ING-002: 4-tuple w/ score
    )
    # The reinforce only fires when the candidate is factually compatible (_is_factually_compatible
    # is fail-closed: normalized content + participants must match). Use a near-duplicate surface form
    # of the seed's "occ probe" so a REAL reinforce runs — otherwise create() inserts a fresh row and
    # this composition assertion silently exercises nothing.
    asyncio.run(plane.create(EpisodicMemory(namespace=ns, content="Occ probe.", state="matured")))

    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 1  # leased increment composed with the reinforce
    assert int(row.get("reinforcement_count", 0)) == rc_before + 1  # the reinforce still landed


def _curated_payload(client: QdrantClient, oid: str) -> dict[str, Any]:
    # DATA-001 P2: after a same-id update the object is a v2 layout; lineage (superseded_by) lives on the
    # ANCHOR identity, not the content shell — exclude content (a no-op for a v1 row).
    from musubi.store.specs import POINT_KIND_CONTENT, POINT_KIND_FIELD

    recs, _ = client.scroll(
        collection_name=_CURATED_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))],
            must_not=[
                models.FieldCondition(
                    key=POINT_KIND_FIELD, match=models.MatchValue(value=POINT_KIND_CONTENT)
                )
            ],
        ),
        limit=1,
        with_payload=True,
    )
    return dict(recs[0].payload or {}) if recs else {}


@pytest.mark.integration
def test_curated_same_id_update_preserves_concurrent_lineage(
    real_qdrant: QdrantClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Curated same-id UPDATE (a vault sync) takes identity + lineage from the FRESH current row, not
    the pre-read snapshot — so a concurrent supersession's ``superseded_by`` is never overwritten
    (DATA-001). Models the read-to-write window by pinning the vault-path probe to a pre-supersession
    snapshot."""
    plane = _wired_curated_plane(real_qdrant, tmp_path / "occ-curated.db")
    ns = f"data001-{generate_ksuid()[:8].lower()}/dev/curated"
    original = asyncio.run(
        plane.create(
            CuratedKnowledge(
                namespace=ns,
                content="original body",
                title="lineage probe",
                vault_path="notes/lin.md",
                body_hash="a" * 64,
            )
        )
    )
    oid = original.object_id
    successor = generate_ksuid()

    # A concurrent supersession sets superseded_by AFTER the vault-sync probe read the row.
    real_qdrant.set_payload(
        collection_name=_CURATED_COLL,
        payload={"superseded_by": successor},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )
    monkeypatch.setattr(
        plane,
        "_find_by_vault_path",
        lambda *, namespace, vault_path: original,  # stale snapshot
    )
    asyncio.run(
        plane.create(
            CuratedKnowledge(
                object_id=oid,
                namespace=ns,
                content="a different body",
                title="lineage probe",
                vault_path="notes/lin.md",
                body_hash="b" * 64,
            )
        )
    )
    assert _curated_payload(real_qdrant, oid).get("superseded_by") == successor
