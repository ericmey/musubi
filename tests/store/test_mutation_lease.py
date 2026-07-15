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


def _run_owned(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Sync test shim: owned_update is async; drive it to completion."""
    return asyncio.run(owned_update(*args, **kwargs))


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
    published = _run_owned(
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
    # An unrelated writer changed importance out from under us.
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"importance": 9},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )
    published = _run_owned(
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
        _run_owned(
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
        _run_owned(
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
    published = _run_owned(
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
    published = _run_owned(
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
        _run_owned(
            real_qdrant,
            _COLL,
            namespace=ns,
            object_id=oid,
            point_id=episodic_point_id(oid),
            plan=lambda cur: MutationPlan(changes={"version": 99}),
        )


@pytest.mark.integration
def test_vanished_row_raises_lookup_error(real_qdrant: QdrantClient) -> None:
    """Review #4: a row that never existed (or vanished) raises MutationRowVanished — a LookupError
    — so callers keep their plane's not-found semantics instead of a model_validate({}) crash."""
    from musubi.store.mutation_lease import MutationRowVanished

    ns = f"ml-{generate_ksuid()[:8].lower()}/main/episodic"
    missing = generate_ksuid()
    with pytest.raises(LookupError):
        _run_owned(
            real_qdrant,
            _COLL,
            namespace=ns,
            object_id=missing,
            point_id=episodic_point_id(missing),
            plan=lambda cur: MutationPlan(changes={"tags": ["x"]}),
        )
    # And it is the typed subclass, so callers can distinguish it if they want.
    with pytest.raises(MutationRowVanished):
        _run_owned(
            real_qdrant,
            _COLL,
            namespace=ns,
            object_id=missing,
            point_id=episodic_point_id(missing),
            plan=lambda cur: MutationPlan(changes={"tags": ["x"]}),
        )


class _CommitRacer:
    """Wraps a real client. On the FIRST commit (the ``set_payload`` whose payload carries
    ``version`` — i.e. the phase-4 commit), it first runs ``race_fn`` via the raw inner client, then
    lets the wrapped commit proceed. Models a takeover landing BETWEEN our acquire and our commit."""

    def __init__(self, inner: QdrantClient, race_fn: Any) -> None:
        self._inner = inner
        self._race_fn = race_fn
        self._raced = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def set_payload(
        self, *, collection_name: str, payload: dict[str, Any], points: Any, **kw: Any
    ) -> Any:
        if "version" in payload and not self._raced:
            self._raced = True
            self._race_fn()
        return self._inner.set_payload(
            collection_name=collection_name, payload=payload, points=points, **kw
        )


@pytest.mark.integration
def test_stalled_owner_does_not_falsely_attribute_a_takeover_commit(
    real_qdrant: QdrantClient,
) -> None:
    """Yua #539 discriminator: A acquires at v1 and stalls; B takes over, publishes a DIFFERENT field
    at v2 and clears; A resumes. With the exact done-token, A's commit (fenced on its own token)
    matches zero, its attribution requires its OWN done token (absent), so A does NOT falsely claim
    B's commit — it retries, recomputes against the fresh row, and lands at v3. BOTH changes survive.
    RED on the old {token==None AND version==read+1} attribution (A would return success, losing its
    change); GREEN with the done-token."""
    ns, oid = _seed(real_qdrant, importance=5)  # v1, tags=[], importance=5

    def b_takes_over() -> None:
        # B's completed takeover: a DIFFERENT field (tags) published at v2, token cleared.
        real_qdrant.set_payload(
            collection_name=_COLL,
            payload={"tags": ["from-b"], "version": 2, "update_lease_token": None},
            points=models.Filter(
                must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
            ),
        )

    racer = cast(QdrantClient, _CommitRacer(real_qdrant, b_takes_over))
    _run_owned(
        racer,
        _COLL,
        namespace=ns,
        object_id=oid,
        point_id=episodic_point_id(oid),
        plan=lambda cur: MutationPlan(changes={"importance": 9}),  # A's change
    )

    row = _payload(real_qdrant, oid)
    assert row["tags"] == ["from-b"], "B's takeover change was lost"
    assert row["importance"] == 9, "A falsely attributed B's commit and lost its own change"
    assert row["version"] == 3, "expected two serialized commits (B at v2, A at v3)"
    assert row.get("update_lease_token") is None


@pytest.mark.integration
def test_crash_after_done_before_clear_recovers_without_reapply(real_qdrant: QdrantClient) -> None:
    """A committed (version bumped, done token stamped) then crashed before clearing. The stale
    EXPIRED done token is taken over by the next writer, which applies ITS change at the next version
    — the committed change is preserved (not lost), not re-applied, and the stale done is cleared."""
    ns, oid = _seed(real_qdrant, importance=5)  # v1
    # Model A's post-commit crash: A's change (tags) committed at v2, done token stamped, never cleared.
    expired_done = f"done:{int(time.time() * 1_000_000) - 10_000_000}:crashedA"
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"tags": ["from-a"], "version": 2, "update_lease_token": expired_done},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )

    _run_owned(
        real_qdrant,
        _COLL,
        namespace=ns,
        object_id=oid,
        point_id=episodic_point_id(oid),
        plan=lambda cur: MutationPlan(changes={"importance": 9}),  # B's change
    )

    row = _payload(real_qdrant, oid)
    assert row["tags"] == [
        "from-a"
    ]  # A's committed change preserved (not lost, not double-applied)
    assert row["importance"] == 9  # B's change applied on top
    assert row["version"] == 3  # B committed at the next version — no regression
    assert row.get("update_lease_token") is None  # the stale done token was cleared


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
        _run_owned(
            cast(Any, _AlwaysLiveOwnerClient()),
            _COLL,
            namespace="n/n/episodic",
            object_id="o",
            point_id="p1",
            plan=lambda cur: MutationPlan(changes={"tags": ["x"]}),
        )
