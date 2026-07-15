"""RET-002 / Issue #500 — final-delivery access accounting (discriminating red matrix).

The invariant Yua locked (rulings 2026-07-15):

    After fanout / dedup / sort / limit, account each FINAL DELIVERED row exactly once;
    never account a dropped candidate; identical whether ``include_lineage`` is true or false.

Scope of "account" (ruling 1): only planes whose type carries ``access_count`` are
accountable — episodic, curated, concept (all extend ``MemoryObject``). artifact and
thought extend ``MusubiObject`` and intentionally lack the field, so their delivered rows
are an EXPLICIT, TESTED no-op — NOT a schema expansion in RET-002.

Every test here reads access data through the NON-MUTATING ``plane.raw_payload`` (or the
raw store lookup), never through ``get()`` — a ``get()`` would bump the very counter under
test (audit harness rule 1: never measure through a mutating surface).

These are RED against current main (5b53693): today accounting is a side-effect of deep
lineage hydration (``EpisodicPlane.get(bump_access=True)`` via ``deep._hydrate_one``), so
fast/recent/curated/concept never mark, deep-without-lineage marks nothing, and the deep
lineage-walk marks rows never delivered. The two no-op guards (artifact/thought) are green
both before and after — they guard against a future regression that starts writing the
field. The implementation flips the reds green in the same slice.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from qdrant_client import QdrantClient

from musubi.embedding.fake import FakeEmbedder
from musubi.planes.artifact.plane import ArtifactPlane
from musubi.planes.concept.plane import ConceptPlane
from musubi.planes.curated.plane import CuratedPlane
from musubi.planes.episodic.plane import EpisodicPlane
from musubi.planes.thoughts.plane import ThoughtsPlane
from musubi.retrieve.orchestration import NamespaceTarget, RetrievalQuery
from musubi.retrieve.orchestration import retrieve as run_orchestration_retrieve
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.store.raw_lookup import raw_payload
from musubi.types.artifact import SourceArtifact
from musubi.types.common import generate_ksuid
from musubi.types.concept import SynthesizedConcept
from musubi.types.curated import CuratedKnowledge
from musubi.types.episodic import EpisodicMemory
from musubi.types.thought import Thought

pytestmark = pytest.mark.asyncio

_ROOT = "eric/claude-code"


class _FakeReranker:
    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        # Descending — preserve pre-sorted order, exercise the rerank branch.
        return [float(1.0 - i * 0.01) for i in range(len(texts))]


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def reranker() -> _FakeReranker:
    return _FakeReranker()


# ── non-mutating access-count reader ──────────────────────────────────────────
def _ac(client: QdrantClient, plane: str, namespace: str, object_id: str) -> int | None:
    """The stored ``access_count`` read straight from the payload — no bump, ever."""
    payload = raw_payload(
        client, collection_for_plane(plane), namespace=namespace, object_id=object_id
    )
    return None if payload is None else payload.get("access_count")


# ── seed helpers (all matured so they clear the default state gate) ────────────
async def _seed_episodic(client: QdrantClient, emb: FakeEmbedder, ns: str, content: str) -> str:
    row = await EpisodicPlane(client=client, embedder=emb).create(
        EpisodicMemory(namespace=ns, content=content, state="matured")
    )
    return row.object_id


async def _seed_curated(client: QdrantClient, emb: FakeEmbedder, ns: str, content: str) -> str:
    row = await CuratedPlane(client=client, embedder=emb).create(
        CuratedKnowledge(
            namespace=ns,
            content=content,
            title="shared curated marker",
            vault_path="notes/marker.md",
            body_hash="d" * 64,
            state="matured",
        )
    )
    return row.object_id


async def _seed_concept(client: QdrantClient, emb: FakeEmbedder, ns: str, content: str) -> str:
    row = await ConceptPlane(client=client, embedder=emb).create(
        SynthesizedConcept(
            namespace=ns,
            content=content,
            title="shared concept marker",
            synthesis_rationale="unit fixture",
            merged_from=[generate_ksuid() for _ in range(3)],
            state="matured",
        )
    )
    return row.object_id


async def _run(
    client: QdrantClient,
    emb: FakeEmbedder,
    rer: Any,
    *,
    ns: str,
    plane: str,
    mode: str,
    limit: int = 25,
    include_lineage: bool = True,
    query_text: str = "shared",
) -> list[Any]:
    """Drive orchestration.retrieve against ONE plane target and return the delivered rows."""
    q = RetrievalQuery(
        namespace=ns,
        query_text=query_text if mode != "recent" else "",
        mode=mode,  # type: ignore[arg-type]
        limit=limit,
        planes=[plane],
        include_lineage=include_lineage,
        state_filter=["provisional", "matured", "promoted"],
        namespace_targets=[NamespaceTarget(namespace=ns, plane=plane)],
    )
    res = await run_orchestration_retrieve(client, emb, rer, query=q)
    assert res.is_ok(), res
    return list(res.unwrap().results)


# ═══ episodic across every mode ═══════════════════════════════════════════════
@pytest.mark.parametrize("mode", ["fast", "deep", "blended", "recent"])
async def test_delivered_episodic_row_accounted_once_per_mode(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker, mode: str
) -> None:
    ns = f"{_ROOT}/episodic"
    oid = await _seed_episodic(qdrant, embedder, ns, "shared marker one")
    assert _ac(qdrant, "episodic", ns, oid) == 0  # created, never accounted

    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="episodic", mode=mode, limit=5)
    assert any(r.object_id == oid for r in rows), f"{mode}: fixture not delivered"

    assert _ac(qdrant, "episodic", ns, oid) == 1, (
        f"{mode}: delivered row must be accounted exactly once"
    )


# ═══ include_lineage parity (the entanglement) ════════════════════════════════
async def test_deep_include_lineage_false_still_accounts_delivered(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    ns = f"{_ROOT}/episodic"
    oid = await _seed_episodic(qdrant, embedder, ns, "shared lineage off")
    await _run(
        qdrant, embedder, reranker, ns=ns, plane="episodic", mode="deep", include_lineage=False
    )
    assert _ac(qdrant, "episodic", ns, oid) == 1  # accounting must NOT depend on hydration


async def test_deep_accounting_identical_regardless_of_include_lineage(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    """Parity: ``include_lineage=False`` must account a delivered row EXACTLY as True does — it is
    not a no-op. One row, one namespace: True accounts it once (0→1), then False accounts it once
    more (1→2). Pre-fix, False skips hydration and never accounts, so the row would stall at 1."""
    ns = f"{_ROOT}/episodic"
    oid = await _seed_episodic(qdrant, embedder, ns, "shared parity marker")
    assert _ac(qdrant, "episodic", ns, oid) == 0

    await _run(
        qdrant, embedder, reranker, ns=ns, plane="episodic", mode="deep", include_lineage=True
    )
    assert _ac(qdrant, "episodic", ns, oid) == 1  # True accounts one delivery

    await _run(
        qdrant, embedder, reranker, ns=ns, plane="episodic", mode="deep", include_lineage=False
    )
    assert _ac(qdrant, "episodic", ns, oid) == 2  # False accounts identically — not a no-op


# ═══ over-marking: a dropped candidate must stay untouched ═════════════════════
async def test_limit_drop_accounts_only_delivered_not_dropped_candidates(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    ns = f"{_ROOT}/episodic"
    oids = [await _seed_episodic(qdrant, embedder, ns, f"shared cohort {i}") for i in range(5)]

    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="episodic", mode="blended", limit=2)
    delivered = {r.object_id for r in rows} & set(oids)
    assert len(delivered) == 2, "probe needs exactly 2 of the cohort delivered"

    for oid in oids:
        expected = 1 if oid in delivered else 0
        assert _ac(qdrant, "episodic", ns, oid) == expected, (
            f"{oid}: delivered={oid in delivered} — dropped candidates must stay 0, "
            f"delivered accounted exactly once"
        )


# ═══ curated + concept are accountable (carry access_count) ═══════════════════
async def test_delivered_curated_row_accounted(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    ns = f"{_ROOT}/curated"
    oid = await _seed_curated(qdrant, embedder, ns, "shared curated marker")
    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="curated", mode="deep", limit=5)
    assert any(r.object_id == oid for r in rows)
    assert _ac(qdrant, "curated", ns, oid) == 1


async def test_delivered_concept_row_accounted(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    ns = f"{_ROOT}/concept"
    oid = await _seed_concept(qdrant, embedder, ns, "shared concept marker")
    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="concept", mode="deep", limit=5)
    assert any(r.object_id == oid for r in rows)
    assert _ac(qdrant, "concept", ns, oid) == 1


# ═══ artifact + thought: explicit tested no-op (types lack access_count) ═══════
async def test_delivered_artifact_row_is_explicit_noop(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    ns = f"{_ROOT}/artifact"
    art = await ArtifactPlane(client=qdrant, embedder=embedder).create(
        SourceArtifact(
            namespace=ns,
            title="shared artifact marker",
            filename="marker.md",
            sha256="a" * 64,
            content_type="text/markdown",
            size_bytes=32,
            chunker="markdown-headings-v1",
        )
    )
    # No access_count field on SourceArtifact — accounting is a deliberate no-op, no error.
    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="artifact", mode="deep", limit=5)
    _ = rows  # delivery may be empty depending on chunking; the invariant is: no access_count write.
    assert _ac(qdrant, "artifact", ns, art.object_id) is None


async def test_delivered_thought_row_is_explicit_noop(
    qdrant: QdrantClient, embedder: FakeEmbedder, reranker: _FakeReranker
) -> None:
    ns = f"{_ROOT}/thought"
    th = await ThoughtsPlane(client=qdrant, embedder=embedder).send(
        Thought(
            namespace=ns, content="shared thought marker", from_presence="aoi", to_presence="yua"
        )
    )
    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="thought", mode="deep", limit=5)
    _ = rows
    assert _ac(qdrant, "thought", ns, th.object_id) is None


# ═══ batched, not N+1 ═════════════════════════════════════════════════════════
async def test_accounting_is_batched_per_collection_not_n_plus_1(
    qdrant: QdrantClient,
    embedder: FakeEmbedder,
    reranker: _FakeReranker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five delivered episodic rows must cost ONE accounting write to the episodic collection,
    not five. Today deep hydration issues one write PER hydrated row (N+1)."""
    ns = f"{_ROOT}/episodic"
    for i in range(5):
        await _seed_episodic(qdrant, embedder, ns, f"shared batch {i}")

    writes: dict[str, int] = {}
    real_batch = qdrant.batch_update_points
    real_set = qdrant.set_payload

    def _count(name: str, real: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            col = kwargs.get("collection_name", "")
            if col == collection_for_plane("episodic"):
                writes[col] = writes.get(col, 0) + 1
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(qdrant, "batch_update_points", _count("batch", real_batch))
    monkeypatch.setattr(qdrant, "set_payload", _count("set", real_set))

    rows = await _run(qdrant, embedder, reranker, ns=ns, plane="episodic", mode="deep", limit=5)
    assert len(rows) == 5
    assert writes.get(collection_for_plane("episodic"), 0) <= 1, (
        "accounting must be one batched write, not N+1"
    )
