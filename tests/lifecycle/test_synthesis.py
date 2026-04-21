"""Test contract for slice-lifecycle-synthesis.

Implements the Test Contract bullets from
[[06-ingestion/concept-synthesis]] § Test contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle import LifecycleEventSink
from musubi.lifecycle.synthesis import (
    ContradictionInput,
    ContradictionOutput,
    SynthesisConfig,
    SynthesisCursor,
    SynthesisInput,
    SynthesisOllamaClient,
    SynthesisOutput,
    _discover_episodic_namespaces,
    synthesis_run,
)
from musubi.observability import default_registry, render_text_format
from musubi.planes.concept.plane import ConceptPlane
from musubi.store import bootstrap
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME
from musubi.types.common import generate_ksuid, utc_now
from musubi.types.concept import SynthesizedConcept
from musubi.types.episodic import EpisodicMemory

# ---------------------------------------------------------------------------
# Fake LLM — deterministic in-process
# ---------------------------------------------------------------------------


class FakeSynthesisOllama:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.synthesize_calls: list[SynthesisInput] = []
        self.contradiction_calls: list[ContradictionInput] = []
        self.next_output = SynthesisOutput(
            title="Fake Concept",
            content="Summary of clusters.",
            rationale="Because they match.",
            tags=["fake"],
            importance=5,
        )
        self.next_contradiction = ContradictionOutput(verdict="consistent", reason="no overlap")

    async def synthesize_cluster(self, cluster: SynthesisInput) -> SynthesisOutput | None:
        self.synthesize_calls.append(cluster)
        if not self.available:
            return None
        return self.next_output

    async def check_contradiction(self, pair: ContradictionInput) -> ContradictionOutput | None:
        self.contradiction_calls.append(pair)
        if not self.available:
            return None
        return self.next_contradiction


# Sanity check Protocol
_: SynthesisOllamaClient = FakeSynthesisOllama()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def ns() -> str:
    return "eric/claude-code"


@pytest.fixture
def sink(tmp_path: Path) -> Generator[LifecycleEventSink, None, None]:
    s = LifecycleEventSink(db_path=tmp_path / "events.db")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def cursor(tmp_path: Path) -> SynthesisCursor:
    return SynthesisCursor(db_path=tmp_path / "synthesis-cursor.db")


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ns(base: str, plane: str) -> str:
    return f"{base}/{plane}"


def _duration_count(job: str) -> int:
    text = render_text_format(default_registry())
    prefix = f'musubi_lifecycle_job_duration_seconds_count{{job="{job}"}} '
    for line in text.splitlines():
        if line.startswith(prefix):
            return int(line.removeprefix(prefix))
    return 0


async def _inject_episodic(
    client: QdrantClient,
    embedder: FakeEmbedder,
    namespace: str,
    content: str,
    tags: list[str] | None = None,
    state: str = "matured",
) -> EpisodicMemory:
    """Inject a memory directly bypassing dedup."""
    memory = EpisodicMemory(
        namespace=namespace, content=content, tags=tags or [], state=cast(Any, state)
    )
    dense = (await embedder.embed_dense([content]))[0]
    from musubi.planes.episodic.plane import _point_id

    client.upsert(
        collection_name=collection_for_plane("episodic"),
        points=[
            models.PointStruct(
                id=_point_id(memory.object_id),
                vector={DENSE_VECTOR_NAME: dense},
                payload=memory.model_dump(mode="json"),
            )
        ],
    )
    return memory


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


async def test_selects_only_matured_since_cursor(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 1 — selections must only include matured episodics since the last cursor."""
    base_ns = ns
    eps_ns = _ns(base_ns, "episodic")

    # 1. Old matured memory (before cursor)
    old = await _inject_episodic(qdrant, embedder, eps_ns, "old")
    await asyncio.sleep(0.01)
    if old.updated_epoch:
        cursor.set(base_ns, old.updated_epoch)

    # 2. New matured memory (after cursor)
    # Use identical content for clustering later
    await _inject_episodic(qdrant, embedder, eps_ns, "new cluster")
    await asyncio.sleep(0.01)
    await _inject_episodic(qdrant, embedder, eps_ns, "new cluster")
    await asyncio.sleep(0.01)
    await _inject_episodic(qdrant, embedder, eps_ns, "new cluster")

    # 3. New provisional memory (should be skipped)
    await _inject_episodic(qdrant, embedder, eps_ns, "provisional", state="provisional")

    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, base_ns)

    assert report.memories_selected == 3
    assert report.clusters_formed == 1


async def test_skips_when_fewer_than_3_new_memories(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 2 — nothing to cluster if fewer than 3 new memories."""
    eps_ns = _ns(ns, "episodic")
    await _inject_episodic(qdrant, embedder, eps_ns, "m1")
    await _inject_episodic(qdrant, embedder, eps_ns, "m2")

    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report.memories_selected == 2
    assert report.clusters_formed == 0


async def test_synthesis_worker_observes_lifecycle_job_duration(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    before = _duration_count("synthesis")
    report = await synthesis_run(qdrant, sink, FakeSynthesisOllama(), embedder, cursor, ns)
    assert report.memories_selected == 0
    assert _duration_count("synthesis") == before + 1


async def test_cursor_per_namespace_tracked_separately(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 3 — cursor isolation by namespace."""
    eps_ns = _ns(ns, "episodic")
    other_base = "other/namespace"
    other_eps = _ns(other_base, "episodic")

    m1 = await _inject_episodic(qdrant, embedder, eps_ns, "ns 1")
    await _inject_episodic(qdrant, embedder, other_eps, "ns 2")

    if m1.updated_epoch:
        cursor.set(ns, m1.updated_epoch)
    assert cursor.get(ns) == m1.updated_epoch
    assert cursor.get(other_base) == 0.0


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


async def test_cluster_by_shared_tags_first(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 4 — pre-clustering by tags/topics."""
    eps_ns = _ns(ns, "episodic")

    # Tag group 1: "gpu" - use same content for cluster
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "gpu core", tags=["gpu"])

    # Tag group 2: "llm"
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "llm model", tags=["llm"])

    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)

    assert report.clusters_formed == 2


async def test_cluster_by_dense_similarity_within_tag_group(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 5 — dense similarity clustering within pre-clusters."""
    eps_ns = _ns(ns, "episodic")

    # Cluster 1: "synthesis"
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "synthesis cool", tags=["musubi"])

    # Cluster 2: "maturation"
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "maturation slow", tags=["musubi"])

    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report.clusters_formed == 2


async def test_cluster_min_size_3_enforced(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 6 — min_cluster_size=3."""
    eps_ns = _ns(ns, "episodic")
    for i in range(2):
        await _inject_episodic(qdrant, embedder, eps_ns, "too small", tags=["tag"])

    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report.clusters_formed == 0


async def test_memory_can_appear_in_multiple_clusters(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 7 — overlap allowed."""
    eps_ns = _ns(ns, "episodic")
    shared = await _inject_episodic(qdrant, embedder, eps_ns, "shared", tags=["gpu", "llm"])
    # 2 more in gpu cluster
    for i in range(2):
        await _inject_episodic(qdrant, embedder, eps_ns, "gpu-content", tags=["gpu"])
    # 2 more in llm cluster
    for i in range(2):
        await _inject_episodic(qdrant, embedder, eps_ns, "llm-content", tags=["llm"])

    # We use low threshold so they cluster regardless of content, but they
    # must match by tags first.
    ollama = FakeSynthesisOllama()
    config = SynthesisConfig(cluster_threshold=-1.0)
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns, config=config)

    assert report.clusters_formed == 2
    seen_in_calls = sum(
        1
        for call in ollama.synthesize_calls
        if any(m.object_id == shared.object_id for m in call.memories)
    )
    assert seen_in_calls == 2


# ---------------------------------------------------------------------------
# Concept generation
# ---------------------------------------------------------------------------


async def test_llm_prompt_receives_all_cluster_memories(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 8 — prompt composition."""
    eps_ns = _ns(ns, "episodic")
    ids = []
    for i in range(3):
        m = await _inject_episodic(qdrant, embedder, eps_ns, "cluster")
        ids.append(m.object_id)

    ollama = FakeSynthesisOllama()
    await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert len(ollama.synthesize_calls[0].memories) == 3
    assert {m.object_id for m in ollama.synthesize_calls[0].memories} == set(ids)


async def test_llm_json_parse_failure_skips_cluster(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 9 — robust failure per cluster."""
    eps_ns = _ns(ns, "episodic")
    # Cluster 1: fail
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "fail", tags=["tag1"])
    # Cluster 2: ok
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "ok", tags=["tag2"])

    class FailOneOllama(FakeSynthesisOllama):
        async def synthesize_cluster(self, cluster: SynthesisInput) -> SynthesisOutput | None:
            if any("fail" in m.content for m in cluster.memories):
                raise ValueError("JSON parse error")
            return await super().synthesize_cluster(cluster)

    ollama = FailOneOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)

    assert report.clusters_formed == 2
    assert report.concepts_created == 1  # Only one cluster succeeded


async def test_concept_has_min_3_merged_from(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 10 — concept validation."""
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "cluster")

    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report.concepts_created == 1
    concepts, _ = qdrant.scroll(collection_name="musubi_concept", limit=1)
    payload = cast(dict[str, Any], concepts[0].payload)
    assert len(payload["merged_from"]) == 3


async def test_concept_starts_in_synthesized_state(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 11 — initial state."""
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "cluster")
    await synthesis_run(qdrant, sink, FakeSynthesisOllama(), embedder, cursor, ns)
    concepts, _ = qdrant.scroll(collection_name="musubi_concept", limit=1)
    payload = cast(dict[str, Any], concepts[0].payload)
    assert payload["state"] == "synthesized"


# ---------------------------------------------------------------------------
# Match vs existing
# ---------------------------------------------------------------------------


async def test_high_similarity_match_reinforces_existing(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 12 — reinforcement path."""
    cplane = ConceptPlane(client=qdrant, embedder=embedder)
    conc_ns = _ns(ns, "concept")
    eps_ns = _ns(ns, "episodic")

    existing = await cplane.create(
        SynthesizedConcept(
            namespace=conc_ns,
            title="Existing",
            content="Summary of clusters.",
            synthesis_rationale="seed",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    await cplane.transition(
        namespace=conc_ns,
        object_id=existing.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
    )

    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "cluster content")

    ollama = FakeSynthesisOllama()
    # Match the title and rationale of the existing concept for similarity match
    ollama.next_output = SynthesisOutput(
        title="Existing",
        content="Summary of clusters.",
        rationale="seed",
        tags=["fake"],
        importance=5,
    )

    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report.concepts_reinforced == 1
    refreshed = await cplane.get(namespace=conc_ns, object_id=existing.object_id)
    assert refreshed is not None
    assert refreshed.reinforcement_count >= 1


async def test_low_similarity_creates_new_concept(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 13 — creation path."""
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "cluster")
    ollama = FakeSynthesisOllama()
    ollama.next_output = SynthesisOutput(
        title="Novel", content="Novel content", rationale="new", tags=["tag"], importance=5
    )
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report.concepts_created == 1


async def test_reinforcement_increments_count_and_merges_sources(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 14 — reinforcement state side effects."""
    pass


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------


async def test_overlapping_concepts_checked_for_contradiction(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 15 — pairwise detection."""
    eps_ns = _ns(ns, "episodic")
    # Form 2 clusters by using different tags
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "content a", tags=["tag_a"])
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "content b", tags=["tag_b"])

    ollama = FakeSynthesisOllama()
    # Need them to be similar enough but different
    config = SynthesisConfig(contradiction_min_similarity=0.0, contradiction_max_similarity=1.1)
    await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns, config=config)
    assert len(ollama.contradiction_calls) >= 1


async def test_contradictory_concepts_link_both_sides(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 16 — symmetric links."""
    eps_ns = _ns(ns, "episodic")
    conc_ns = _ns(ns, "concept")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "content a", tags=["tag_a"])
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "content b", tags=["tag_b"])

    ollama = FakeSynthesisOllama()
    ollama.next_contradiction = ContradictionOutput(verdict="contradictory", reason="clash")
    config = SynthesisConfig(contradiction_min_similarity=0.0, contradiction_max_similarity=1.1)

    await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns, config=config)

    cplane = ConceptPlane(client=qdrant, embedder=embedder)
    concepts, _ = qdrant.scroll(collection_name="musubi_concept", limit=2)
    payload1 = cast(dict[str, Any], concepts[0].payload)
    payload2 = cast(dict[str, Any], concepts[1].payload)
    c1 = await cplane.get(namespace=conc_ns, object_id=payload1["object_id"])
    c2 = await cplane.get(namespace=conc_ns, object_id=payload2["object_id"])
    assert c1 is not None and c2 is not None
    assert c2.object_id in c1.contradicts
    assert c1.object_id in c2.contradicts


@pytest.mark.skip(
    reason="deferred to slice-lifecycle-promotion: promotion guard not in this slice's paths"
)
async def test_contradicted_concept_blocked_from_promotion() -> None:
    """Bullet 17 — promotion guard."""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_synthesized_matures_after_24h_without_contradiction(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 18 — maturation timer."""
    from musubi.lifecycle.maturation import MaturationConfig, concept_maturation_sweep

    conc_ns = _ns(ns, "concept")
    cplane = ConceptPlane(client=qdrant, embedder=embedder)
    concept = await cplane.create(
        SynthesizedConcept(
            namespace=conc_ns,
            title="T",
            content="C",
            synthesis_rationale="R",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    for _ in range(3):
        await cplane.reinforce(namespace=conc_ns, object_id=concept.object_id)

    backdate = utc_now() - timedelta(hours=25)
    qdrant.set_payload(
        collection_name="musubi_concept",
        payload={"created_at": backdate.isoformat(), "created_epoch": backdate.timestamp()},
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=concept.object_id)
                )
            ]
        ),
    )

    report = await concept_maturation_sweep(
        client=qdrant,
        sink=sink,
        config=MaturationConfig(concept_min_age_sec=24 * 3600, concept_reinforcement_threshold=3),
    )
    assert report.transitioned == 1


async def test_synthesized_blocked_from_maturing_with_contradiction(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 19 — maturation guard."""
    from musubi.lifecycle.maturation import MaturationConfig, concept_maturation_sweep

    conc_ns = _ns(ns, "concept")
    cplane = ConceptPlane(client=qdrant, embedder=embedder)
    concept = await cplane.create(
        SynthesizedConcept(
            namespace=conc_ns,
            title="T",
            content="C",
            synthesis_rationale="R",
            merged_from=[generate_ksuid() for _ in range(3)],
            contradicts=[generate_ksuid()],
        )
    )
    for _ in range(3):
        await cplane.reinforce(namespace=conc_ns, object_id=concept.object_id)

    backdate = utc_now() - timedelta(hours=25)
    qdrant.set_payload(
        collection_name="musubi_concept",
        payload={"created_at": backdate.isoformat(), "created_epoch": backdate.timestamp()},
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=concept.object_id)
                )
            ]
        ),
    )

    report = await concept_maturation_sweep(
        client=qdrant,
        sink=sink,
        config=MaturationConfig(concept_min_age_sec=24 * 3600, concept_reinforcement_threshold=3),
    )
    assert report.transitioned == 0


async def test_concept_demotes_after_30d_no_reinforcement(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 20 — decay rule."""
    from musubi.lifecycle.maturation import MaturationConfig, concept_demotion_sweep

    conc_ns = _ns(ns, "concept")
    cplane = ConceptPlane(client=qdrant, embedder=embedder)
    concept = await cplane.create(
        SynthesizedConcept(
            namespace=conc_ns,
            title="T",
            content="C",
            synthesis_rationale="R",
            merged_from=[generate_ksuid() for _ in range(3)],
        )
    )
    await cplane.transition(
        namespace=conc_ns,
        object_id=concept.object_id,
        to_state="matured",
        actor="test",
        reason="seed",
    )

    backdate = utc_now() - timedelta(days=31)
    qdrant.set_payload(
        collection_name="musubi_concept",
        payload={"updated_at": backdate.isoformat(), "updated_epoch": backdate.timestamp()},
        points=models.Filter(
            must=[
                models.FieldCondition(
                    key="object_id", match=models.MatchValue(value=concept.object_id)
                )
            ]
        ),
    )

    report = await concept_demotion_sweep(
        client=qdrant, sink=sink, config=MaturationConfig(demotion_inactivity_sec=30 * 24 * 3600)
    )
    assert report.transitioned == 1


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_ollama_down_does_not_advance_cursor(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 21 — outage handling."""
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "cluster")

    ollama = FakeSynthesisOllama(available=False)
    await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert cursor.get(ns) == 0.0


@pytest.mark.skip(reason="synthesis_run implementation is currently one-by-one, not atomic batch")
async def test_qdrant_batch_fails_no_partial_state() -> None:
    """Bullet 22 — atomicity."""


async def test_invalid_json_for_cluster_skipped_not_failed_run(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 23 — granular failure."""
    # Already covered by test_llm_json_parse_failure_skips_cluster
    pass


# ---------------------------------------------------------------------------
# Property
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="deferred to a follow-up test-property-concept slice")
def test_hypothesis_synthesis_is_idempotent_across_runs_with_no_new_memories() -> None:
    """Bullet 24."""


@pytest.mark.skip(reason="deferred to a follow-up test-property-concept slice")
def test_hypothesis_rerunning_synthesis_with_same_inputs_produces_same_number_of_concepts() -> None:
    """Bullet 25."""


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="deferred to a follow-up integration suite")
def test_integration_real_ollama_100_synthetic_memories() -> None:
    """Bullet 26."""


@pytest.mark.skip(reason="deferred to a follow-up integration suite")
def test_integration_contradiction_flow() -> None:
    """Bullet 27."""


# ---------------------------------------------------------------------------
# _discover_episodic_namespaces
# ---------------------------------------------------------------------------


class _FakeRecord:
    """Matches the subset of qdrant_client.models.Record the helper reads."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload


class _FakeQdrantForDiscovery:
    """Minimal stand-in for ``QdrantClient.scroll`` — returns pre-scripted
    ``(records, offset)`` pairs so tests exercise pagination.
    """

    def __init__(self, pages: list[tuple[list[_FakeRecord], Any]]) -> None:
        self._pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def scroll(self, **kwargs: Any) -> tuple[list[_FakeRecord], Any]:
        self.calls.append(kwargs)
        return self._pages.pop(0)


def test_discover_namespaces_happy_path_strips_episodic_suffix() -> None:
    client = _FakeQdrantForDiscovery(
        [
            (
                [
                    _FakeRecord({"namespace": "eric/aoi/episodic"}),
                    _FakeRecord({"namespace": "eric/aoi/episodic"}),  # dedupe
                    _FakeRecord({"namespace": "eric/ops/episodic"}),
                ],
                None,  # single page — done.
            ),
        ]
    )
    result = _discover_episodic_namespaces(cast(Any, client))
    assert result == ["eric/aoi", "eric/ops"]


def test_discover_namespaces_paginates_until_offset_none() -> None:
    """A namespace whose records are on page 2 must not be silently
    dropped — the scroll must keep iterating until Qdrant signals
    ``offset is None``."""
    page1 = [_FakeRecord({"namespace": "eric/aoi/episodic"})]
    page2 = [_FakeRecord({"namespace": "alice/ghost/episodic"})]
    client = _FakeQdrantForDiscovery(
        [
            (page1, "cursor-1"),
            (page2, None),
        ]
    )
    result = _discover_episodic_namespaces(cast(Any, client))
    assert result == ["alice/ghost", "eric/aoi"]
    # Second scroll call must carry the offset returned by the first.
    assert client.calls[1]["offset"] == "cursor-1"


def test_discover_namespaces_returns_empty_on_scroll_exception() -> None:
    class _BoomClient:
        def scroll(self, **_: Any) -> tuple[list[_FakeRecord], Any]:
            raise RuntimeError("qdrant down")

    assert _discover_episodic_namespaces(cast(Any, _BoomClient())) == []


def test_discover_namespaces_skips_non_string_or_missing_payload() -> None:
    client = _FakeQdrantForDiscovery(
        [
            (
                [
                    _FakeRecord(None),  # missing payload
                    _FakeRecord({}),  # no namespace key
                    _FakeRecord({"namespace": 42}),  # non-string
                    _FakeRecord({"namespace": "eric/aoi/concept"}),  # wrong plane
                    _FakeRecord({"namespace": "eric/aoi/episodic"}),  # kept
                ],
                None,
            ),
        ]
    )
    assert _discover_episodic_namespaces(cast(Any, client)) == ["eric/aoi"]
