"""RET-011 / #510 — real-Qdrant exact deployment-namespace proof.

The unit matrix runs against ``:memory:`` Qdrant, where the top-level fusion filter is not
applied to prefetch candidates. This proves the SAME invariant against a REAL Qdrant server:
a concrete deployment-namespace target must return ONLY that presence's rows, never a sibling
presence in the same identity family, even when their vectors are identical.

Marked ``integration`` — excluded from the default unit run. Bring the server up with
``MUSUBI_TEST_QDRANT_PORT=6339 docker compose -f deploy/test-env/docker-compose.test.yml up -d
qdrant --wait`` (or ``make test-integration-up``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.retrieve.orchestration import NamespaceTarget, RetrievalQuery
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.store import bootstrap
from musubi.types.common import generate_ksuid
from musubi.types.episodic import EpisodicMemory

pytestmark = pytest.mark.asyncio

# Unique per-run tenant so repeated integration runs against the persistent server never collide.
_TENANT = f"ret011-{generate_ksuid()[:8].lower()}"
_PRES_A = f"{_TENANT}/presalpha/episodic"
_PRES_B = f"{_TENANT}/presbravo/episodic"
_CONTENT = "identical real-qdrant marker content stored verbatim by both presences"


@pytest.fixture
def real_qdrant() -> Iterator[QdrantClient]:
    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.integration
async def test_concrete_target_exact_namespace_real_qdrant(real_qdrant: QdrantClient) -> None:
    embedder = FakeEmbedder()
    plane = EpisodicPlane(client=real_qdrant, embedder=embedder)
    a = await plane.create(EpisodicMemory(namespace=_PRES_A, content=_CONTENT, state="matured"))
    b = await plane.create(EpisodicMemory(namespace=_PRES_B, content=_CONTENT, state="matured"))

    q = RetrievalQuery(
        namespace=_PRES_A,
        query_text="identical real-qdrant marker",
        mode="fast",
        limit=10,
        planes=["episodic"],
        state_filter=["provisional", "matured", "promoted"],
        namespace_targets=[NamespaceTarget(namespace=_PRES_A, plane="episodic")],
    )
    res = await run_orchestration_retrieve(real_qdrant, embedder, query=q)  # fast: no reranker
    assert res.is_ok(), res
    rows = list(res.unwrap().results)

    ids = {r.object_id for r in rows}
    namespaces = {r.namespace for r in rows}
    assert a.object_id in ids, "the targeted presence's own row must be returned"
    assert namespaces == {_PRES_A}, (
        f"real Qdrant leaked sibling presence(s): {namespaces - {_PRES_A}}"
    )
    assert b.object_id not in ids, "sibling presence must never be delivered against real Qdrant"
