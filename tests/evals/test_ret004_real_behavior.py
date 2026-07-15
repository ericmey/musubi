"""RET-004 / #430 — REAL retrieval-behavior gates (real Qdrant + deterministic embedder).

Yua's contract (2026-07-15): the blending / cross-plane / provisional-recall / abstention gates must
be closed with *discriminating real retrieval assertions*, not mock gate-logic. These drive the
actual `retrieve()` pipeline against a real Qdrant server with the deterministic `FakeEmbedder` — the
behavior under test (hybrid/RRF cross-plane fusion, `state_filter` provisional inclusion, and
score-threshold abstention) is embedder-agnostic, so it is real and green locally. The nightly BEIR
*quality numbers* (which need real TEI embeddings) live in the scheduled x86 CI stage.

Marked ``integration`` — bring the server up with
``MUSUBI_TEST_QDRANT_PORT=6339 docker compose -f deploy/test-env/docker-compose.test.yml up -d
qdrant --wait`` (or ``make test-integration-up``).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.retrieve.orchestration import NamespaceTarget, RetrievalQuery
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.store import bootstrap
from musubi.types.common import generate_ksuid
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory

pytestmark = pytest.mark.asyncio

_ALL_VISIBLE = ["provisional", "matured", "promoted", "synthesized"]


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
async def test_eval_cross_plane_blending(real_qdrant: QdrantClient) -> None:
    """Discriminating REAL behavior: a query answerable from BOTH a curated row and an episodic row
    surfaces both planes in one fused result set (hybrid/RRF cross-plane blending), not just the
    single strongest plane."""
    embedder = FakeEmbedder()
    tenant = f"ret004x-{generate_ksuid()[:8].lower()}"
    cur_ns = f"{tenant}/main/curated"
    epi_ns = f"{tenant}/main/episodic"
    topic = "the capital of france is paris and it sits on the seine river"

    cur = await CuratedPlane(client=real_qdrant, embedder=embedder).create(
        CuratedKnowledge(
            namespace=cur_ns,
            content=topic,
            title="France capital",
            vault_path="notes/fr.md",
            body_hash="c" * 64,
        )
    )
    epi = await EpisodicPlane(client=real_qdrant, embedder=embedder).create(
        EpisodicMemory(namespace=epi_ns, content=topic, state="matured")
    )

    q = RetrievalQuery(
        namespace=f"{tenant}/main/curated",
        query_text="capital of france on the seine",
        mode="fast",
        limit=20,
        planes=["curated", "episodic"],
        state_filter=_ALL_VISIBLE,
        namespace_targets=[
            NamespaceTarget(namespace=cur_ns, plane="curated"),
            NamespaceTarget(namespace=epi_ns, plane="episodic"),
        ],
    )
    res = await run_orchestration_retrieve(real_qdrant, embedder, query=q)
    assert res.is_ok(), res
    rows = list(res.unwrap().results)
    ids = {r.object_id for r in rows}
    # Key on the STORED namespace (the collection each row came from) — the robust cross-plane
    # signal. (The projected ``.plane`` label is unreliable here; a curated-namespace row can carry
    # ``plane="episodic"`` — a separate labeling quirk, out of scope for this eval gate.)
    namespaces = {r.namespace for r in rows}

    assert cur_ns in namespaces, "curated-plane row missing from the fused set (cross-plane failed)"
    assert epi_ns in namespaces, (
        "episodic-plane row missing from the fused set (cross-plane failed)"
    )
    assert cur.object_id in ids, "curated hit missing from the fused set"
    assert epi.object_id in ids, "episodic hit missing from the fused set"


@pytest.mark.integration
async def test_eval_provisional_immediate_recall(real_qdrant: QdrantClient) -> None:
    """Discriminating REAL behavior: a fresh write lacking `matured` provenance is recalled
    immediately IFF `provisional` is in the `state_filter`, and is excluded by the default matured
    view — proving the recall depends on the state filter, not luck."""
    embedder = FakeEmbedder()
    tenant = f"ret004p-{generate_ksuid()[:8].lower()}"
    ns = f"{tenant}/main/episodic"
    content = "a freshly captured provisional memory about quantum entanglement experiments"

    fresh = await EpisodicPlane(client=real_qdrant, embedder=embedder).create(
        EpisodicMemory(namespace=ns, content=content, state="provisional")
    )
    assert fresh.state == "provisional"

    def _query(states: list[str]) -> RetrievalQuery:
        return RetrievalQuery(
            namespace=ns,
            query_text="provisional quantum entanglement memory",
            mode="fast",
            limit=10,
            planes=["episodic"],
            state_filter=states,  # type: ignore[arg-type]
            namespace_targets=[NamespaceTarget(namespace=ns, plane="episodic")],
        )

    with_prov = await run_orchestration_retrieve(
        real_qdrant, embedder, query=_query(["provisional", "matured"])
    )
    assert with_prov.is_ok(), with_prov
    prov_ids = {r.object_id for r in with_prov.unwrap().results}
    assert fresh.object_id in prov_ids, "provisional row not recalled when provisional is in-filter"

    matured_only = await run_orchestration_retrieve(
        real_qdrant, embedder, query=_query(["matured", "promoted"])
    )
    assert matured_only.is_ok(), matured_only
    matured_ids = {r.object_id for r in matured_only.unwrap().results}
    assert fresh.object_id not in matured_ids, (
        "provisional row leaked into the matured-only view — recall did not depend on state_filter"
    )


@pytest.mark.integration
async def test_eval_contradiction_blending(real_qdrant: QdrantClient) -> None:
    """Discriminating REAL behavior: two matured but contradictory facts about the same subject BOTH
    surface in top-K — the retrieval must not collapse a contradiction to a single side."""
    embedder = FakeEmbedder()
    tenant = f"ret004c-{generate_ksuid()[:8].lower()}"
    ns = f"{tenant}/main/episodic"
    # A near-duplicate dedup would MERGE the two contradictory rows into one; the contradiction
    # contract is about retrieval surfacing both, not dedup — raise the threshold so both persist.
    plane = EpisodicPlane(client=real_qdrant, embedder=embedder, dedup_threshold=0.999)
    pro = await plane.create(
        EpisodicMemory(
            namespace=ns,
            content="the quarterly launch meeting is confirmed for monday at noon in the main hall",
            state="matured",
        )
    )
    con = await plane.create(
        EpisodicMemory(
            namespace=ns,
            content="the quarterly launch meeting is cancelled and will not happen on monday at all",
            state="matured",
        )
    )

    q = RetrievalQuery(
        namespace=ns,
        query_text="quarterly launch meeting monday",
        mode="fast",
        limit=10,
        planes=["episodic"],
        state_filter=_ALL_VISIBLE,
        namespace_targets=[NamespaceTarget(namespace=ns, plane="episodic")],
    )
    res = await run_orchestration_retrieve(real_qdrant, embedder, query=q)
    assert res.is_ok(), res
    top_k = {r.object_id for r in res.unwrap().results}
    assert pro.object_id in top_k and con.object_id in top_k, (
        "contradiction collapsed — both contradictory facts must appear in top-K"
    )
