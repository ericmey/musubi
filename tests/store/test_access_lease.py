"""RET-008 / #502 — fenced per-record lease invariants.

Integration tests manipulate ``access_lease_token`` directly on a real Qdrant server and drive
:func:`musubi.store.access_lease.lease_increment_access`; the exhaustion + single-loop guards are
unit tests. Bring the server up with ``make test-integration-up`` (port 6339).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.store import bootstrap
from musubi.store.access_lease import AccessLeaseExhausted, lease_increment_access
from musubi.store.names import collection_for_plane
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


def _seed(client: QdrantClient) -> tuple[str, str]:
    ns = f"lease-{generate_ksuid()[:8].lower()}/dev/episodic"
    row = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="lease invariant", state="matured")
        )
    )
    return ns, row.object_id


def _row(client: QdrantClient, oid: str) -> dict[str, Any]:
    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
        limit=1,
        with_payload=True,
    )
    return dict(recs[0].payload or {}) if recs else {}


def _set_token(client: QdrantClient, ns: str, oid: str, token: str) -> None:
    client.set_payload(
        collection_name=_COLL,
        payload={"access_lease_token": token},
        points=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=oid))]
        ),
    )


@pytest.mark.integration
def test_update_and_release_atomic_readback(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant)
    lease_increment_access(real_qdrant, _COLL, {(ns, oid)})
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 1  # incremented
    assert row.get("access_lease_token") is None  # released in the same fenced update


@pytest.mark.integration
def test_nonexpired_lease_cannot_be_stolen(real_qdrant: QdrantClient) -> None:
    ns, oid = _seed(real_qdrant)
    fresh = f"{int(time.time() * 1_000_000)}:heldbyanother"  # issued NOW → live
    _set_token(real_qdrant, ns, oid, fresh)
    # A live foreign lease must never be stolen — the increment can't proceed and fails loud
    # rather than corrupting the counter.
    with pytest.raises(AccessLeaseExhausted):
        lease_increment_access(real_qdrant, _COLL, {(ns, oid)})
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 0  # untouched
    assert row.get("access_lease_token") == fresh  # the live lease is intact


@pytest.mark.integration
def test_expired_lease_exact_token_takeover_recovers(real_qdrant: QdrantClient) -> None:
    """A crashed holder leaves an EXPIRED token; the next writer takes over that exact token and
    completes the increment (crash-between-acquire-and-update recovery)."""
    ns, oid = _seed(real_qdrant)
    expired = f"{int(time.time() * 1_000_000) - 10_000_000}:crashedholder"  # 10s ago → expired
    _set_token(real_qdrant, ns, oid, expired)
    lease_increment_access(real_qdrant, _COLL, {(ns, oid)})
    row = _row(real_qdrant, oid)
    assert row.get("access_count") == 1  # taken over + incremented
    assert row.get("access_lease_token") is None  # released


@pytest.mark.integration
def test_old_holder_fenced_after_takeover(real_qdrant: QdrantClient) -> None:
    """After an expired token is taken over, the OLD holder's fenced write (on its old token) must
    match zero points — it can never corrupt the counter post-takeover."""
    ns, oid = _seed(real_qdrant)
    old = f"{int(time.time() * 1_000_000) - 10_000_000}:oldholder"
    _set_token(real_qdrant, ns, oid, old)
    lease_increment_access(real_qdrant, _COLL, {(ns, oid)})  # takes over `old`, count → 1, released
    # The old holder resumes and tries its fenced increment+release on its stale token:
    real_qdrant.set_payload(
        collection_name=_COLL,
        payload={"access_count": 999, "access_lease_token": None},
        points=models.Filter(
            must=[
                models.FieldCondition(key="object_id", match=models.MatchValue(value=oid)),
                models.FieldCondition(key="access_lease_token", match=models.MatchValue(value=old)),
            ]
        ),
    )
    assert _row(real_qdrant, oid).get("access_count") == 1  # old holder's write matched zero


# ── unit guards ───────────────────────────────────────────────────────────────
class _AlwaysLiveLeaseClient:
    """Fake client whose rows always show a FRESH foreign lease → acquire can never win."""

    def scroll(self, *_a: Any, **_k: Any) -> Any:
        token = f"{int(time.time() * 1_000_000)}:foreign"
        rec = type(
            "R",
            (),
            {
                "id": "p1",
                "payload": {
                    "namespace": "n/n/episodic",
                    "object_id": "o",
                    "access_lease_token": token,
                    "access_count": 0,
                },
            },
        )()
        return ([rec], None)

    def batch_update_points(self, *_a: Any, **_k: Any) -> Any:
        return None


def test_lease_exhaustion_is_fail_loud() -> None:
    with pytest.raises(AccessLeaseExhausted):
        lease_increment_access(cast(Any, _AlwaysLiveLeaseClient()), _COLL, {("n/n/episodic", "o")})


@pytest.mark.asyncio
async def test_single_loop_deliveries_stay_correct() -> None:
    """GUARD: within one event loop the sync client blocks across the read→write, so concurrent
    deliveries already serialize — this must stay true (no lease contention needed)."""
    from types import SimpleNamespace

    from musubi.retrieve.accounting import account_delivered

    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        ns = "eric/dev/episodic"
        row = await EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="single loop", state="matured")
        )
        d = SimpleNamespace(plane="episodic", object_id=row.object_id, namespace=ns)
        await asyncio.gather(*[account_delivered(client, [d]) for _ in range(5)])
        assert _row(client, row.object_id).get("access_count") == 5
    finally:
        client.close()
