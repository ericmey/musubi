"""RET-008 / #502 — concurrency-safe access accounting, real-Qdrant proofs.

The read-modify-write race only manifests under REAL parallelism (multiple OS threads /
processes / a future async client / a concurrent cross-process writer) — within one event
loop the synchronous Qdrant client blocks the loop across the whole read→write, so deliveries
serialize (see the single-loop guard in the unit suite). These proofs therefore use real OS
threads against a real Qdrant server.

Marked ``integration`` — bring the server up with
``MUSUBI_TEST_QDRANT_PORT=6339 docker compose -f deploy/test-env/docker-compose.test.yml up -d
qdrant --wait`` (or ``make test-integration-up``).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.retrieve.accounting import account_delivered
from musubi.store import bootstrap
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
    ns = f"ret008-{generate_ksuid()[:8].lower()}/dev/episodic"
    row = asyncio.run(
        EpisodicPlane(client=client, embedder=FakeEmbedder()).create(
            EpisodicMemory(namespace=ns, content="concurrency probe", state="matured")
        )
    )
    return ns, row.object_id


def _count(client: QdrantClient, object_id: str) -> int:
    recs, _ = client.scroll(
        collection_name=_COLL,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(key="object_id", match=models.MatchValue(value=object_id))]
        ),
        limit=1,
        with_payload=True,
    )
    return (recs[0].payload or {}).get("access_count", 0) if recs else -1


def _deliver_once(client: QdrantClient, ns: str, object_id: str) -> None:
    row = SimpleNamespace(plane="episodic", object_id=object_id, namespace=ns)
    asyncio.run(account_delivered(client, [row]))


def _run_parallel_deliveries(client: QdrantClient, ns: str, object_id: str, n: int) -> None:
    barrier = threading.Barrier(n)  # maximize overlap so a lost-update race is forced

    def worker() -> None:
        barrier.wait()
        _deliver_once(client, ns, object_id)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


@pytest.mark.integration
def test_parallel_deliveries_lose_no_increment(real_qdrant: QdrantClient) -> None:
    """RED on the old batched RMW (lost updates under real OS-thread parallelism), green on CAS:
    N genuinely parallel deliveries of one row must yield exactly N accounted accesses."""
    ns, oid = _seed(real_qdrant)
    _run_parallel_deliveries(real_qdrant, ns, oid, n=8)
    assert _count(real_qdrant, oid) == 8, "parallel deliveries lost an increment (RMW race)"


@pytest.mark.integration
def test_eight_way_delivery_final_count_exact(real_qdrant: QdrantClient) -> None:
    """Two independent runs of 8 parallel deliveries → the counter is exact and monotonic (16)."""
    ns, oid = _seed(real_qdrant)
    _run_parallel_deliveries(real_qdrant, ns, oid, n=8)
    _run_parallel_deliveries(real_qdrant, ns, oid, n=8)
    assert _count(real_qdrant, oid) == 16
