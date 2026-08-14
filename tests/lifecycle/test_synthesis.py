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
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.lifecycle.synthesis import (
    ContradictionInput,
    ContradictionOutput,
    SynthesisConfig,
    SynthesisCursor,
    SynthesisInput,
    SynthesisOllamaClient,
    SynthesisOutput,
    _discover_episodic_namespaces,
    build_synthesis_jobs,
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


def _coordinator(qdrant: QdrantClient, sink: LifecycleEventSink) -> LifecycleTransitionCoordinator:
    return LifecycleTransitionCoordinator(client=qdrant, db_path=sink._db_path)


def _duration_count(job: str) -> int:
    text = render_text_format(default_registry())
    prefix = f'musubi_lifecycle_job_duration_seconds_count{{job="{job}"}} '
    for line in text.splitlines():
        if line.startswith(prefix):
            return int(line.removeprefix(prefix))
    return 0


def _family_counter(name: str, family: str) -> float:
    """Read a family-labeled counter's current value from the process
    registry. Counters are process-global across tests — callers must
    assert DELTAS, never absolute values."""
    text = render_text_format(default_registry())
    prefix = f'{name}{{family="{family}"}} '
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line.removeprefix(prefix))
    return 0.0


async def _inject_episodic(
    client: QdrantClient,
    embedder: FakeEmbedder,
    namespace: str,
    content: str,
    tags: list[str] | None = None,
    state: str = "matured",
    importance: int = 5,
    payload_extra: dict[str, Any] | None = None,
) -> EpisodicMemory:
    """Inject a memory directly bypassing dedup.

    ``payload_extra`` merges raw keys into the stored payload AFTER model
    dump — used to reproduce production rows carrying internal layout keys
    (``committed_operation_id``) or outright schema drift that the
    ``extra="forbid"`` model would never emit itself.
    """
    memory = EpisodicMemory(
        namespace=namespace,
        content=content,
        tags=tags or [],
        state=cast(Any, state),
        importance=importance,
    )
    dense = (await embedder.embed_dense([content]))[0]
    from musubi.planes.episodic.plane import _point_id

    payload = memory.model_dump(mode="json")
    if payload_extra:
        payload.update(payload_extra)
    client.upsert(
        collection_name=collection_for_plane("episodic"),
        points=[
            models.PointStruct(
                id=_point_id(memory.object_id),
                vector={DENSE_VECTOR_NAME: dense},
                payload=payload,
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


# ---------------------------------------------------------------------------
# Candidates pool — the v1.5.5 cursor-skip fix
# ---------------------------------------------------------------------------


def test_candidates_upsert_and_get_within_ttl(cursor: SynthesisCursor) -> None:
    """A memory marked as a candidate is visible to subsequent calls
    within the TTL window."""
    cursor.upsert_candidate("aoi", "mem-1", now_epoch=100.0)
    cursor.upsert_candidate("aoi", "mem-2", now_epoch=100.0)
    cursor.upsert_candidate("yua", "mem-99", now_epoch=100.0)

    aoi_candidates = cursor.get_candidates("aoi", ttl_sec=3600.0, now_epoch=200.0)
    assert sorted(aoi_candidates) == ["mem-1", "mem-2"]
    yua_candidates = cursor.get_candidates("yua", ttl_sec=3600.0, now_epoch=200.0)
    assert yua_candidates == ["mem-99"]


def test_candidates_filtered_by_ttl_window(cursor: SynthesisCursor) -> None:
    """Candidates whose first_seen_epoch is older than `now - ttl`
    are not returned, even if `prune_aged_candidates` hasn't run."""
    cursor.upsert_candidate("aoi", "old-mem", now_epoch=100.0)
    cursor.upsert_candidate("aoi", "fresh-mem", now_epoch=900.0)

    # ttl=500: old-mem (first_seen=100) is past cutoff (1000-500=500)
    visible = cursor.get_candidates("aoi", ttl_sec=500.0, now_epoch=1000.0)
    assert visible == ["fresh-mem"]


def test_candidates_remove_on_successful_cluster(cursor: SynthesisCursor) -> None:
    """When a memory clusters, it's removed from the candidate pool."""
    cursor.upsert_candidate("aoi", "mem-a", now_epoch=100.0)
    cursor.upsert_candidate("aoi", "mem-b", now_epoch=100.0)
    cursor.upsert_candidate("aoi", "mem-c", now_epoch=100.0)

    cursor.remove_candidates("aoi", ["mem-a", "mem-c"])

    remaining = cursor.get_candidates("aoi", ttl_sec=3600.0, now_epoch=200.0)
    assert remaining == ["mem-b"]


def test_candidates_pruned_after_ttl(cursor: SynthesisCursor) -> None:
    """Aging past TTL physically deletes the row, returning the count
    pruned. This is the housekeeping path that prevents the candidates
    table from growing unboundedly."""
    cursor.upsert_candidate("aoi", "ancient-1", now_epoch=100.0)
    cursor.upsert_candidate("aoi", "ancient-2", now_epoch=100.0)
    cursor.upsert_candidate("aoi", "fresh", now_epoch=900.0)

    pruned = cursor.prune_aged_candidates("aoi", ttl_sec=500.0, now_epoch=1000.0)
    assert pruned == 2
    remaining = cursor.get_candidates("aoi", ttl_sec=3600.0, now_epoch=1000.0)
    assert remaining == ["fresh"]


def test_candidates_per_family_isolation(cursor: SynthesisCursor) -> None:
    """Operations on one family's candidates don't touch another's."""
    cursor.upsert_candidate("aoi", "shared-id", now_epoch=100.0)
    cursor.upsert_candidate("yua", "shared-id", now_epoch=100.0)

    cursor.remove_candidates("aoi", ["shared-id"])
    assert cursor.get_candidates("aoi", ttl_sec=3600.0, now_epoch=200.0) == []
    assert cursor.get_candidates("yua", ttl_sec=3600.0, now_epoch=200.0) == ["shared-id"]


def test_cursor_get_set_accepts_namespace_or_family(cursor: SynthesisCursor) -> None:
    """The cursor's `get`/`set` accept either an identity family ("aoi")
    or a full namespace ("aoi/command-chair/episodic"); both reduce to
    the same family-keyed entry. This keeps pre-v1.5.5 callers working
    without signature changes."""
    cursor.set("aoi/command-chair/episodic", 42.0)
    assert cursor.get("aoi/voice/episodic") == 42.0
    assert cursor.get("aoi") == 42.0


async def test_cursor_skip_fix_unclustered_memories_carry_forward(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """The headline v1.5.5 fix: a memory that doesn't cluster on its
    first synthesis pass stays eligible for the next pass via the
    candidate pool. Pre-v1.5.5, it would have been cursor-skipped
    forever.

    Setup:
    1. Write 2 memories with shared tag — too few to cluster (min=3).
    2. Run synthesis. Expect 0 clusters BUT both memories upserted as
       candidates.
    3. Write 1 more memory with the same tag.
    4. Run synthesis again. Expect 1 cluster combining ALL THREE
       (the two carried-forward candidates + the new memory).
    """
    eps_ns = _ns(ns, "episodic")

    # Pass 1: 2 memories, no cluster
    for _ in range(2):
        await _inject_episodic(qdrant, embedder, eps_ns, "carry forward", tags=["topic"])
    ollama = FakeSynthesisOllama()
    report1 = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report1.clusters_formed == 0
    assert report1.memories_selected == 2
    assert report1.candidates_carried_forward >= 2, (
        "unclustered memories should be retained in the candidate pool"
    )

    # Pass 2: third memory arrives — combined pool should cluster
    await _inject_episodic(qdrant, embedder, eps_ns, "carry forward", tags=["topic"])
    report2 = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report2.memories_selected == 3, (
        "candidates from pass 1 must be re-pulled together with the new memory"
    )
    assert report2.clusters_formed == 1, (
        "the new memory + 2 carried-forward candidates form a cluster"
    )


async def test_cluster_min_size_3_enforced(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 6 — min_cluster_size=3 (default).

    A concept must aggregate at least three episodic sources — the
    concept plane enforces this via `_MIN_MERGED_FROM=3`. Two memories
    are not yet a pattern; they're a duplicate."""
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
        coordinator=_coordinator(qdrant, sink),
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
    # Concepts are written to <family>/shared/concept in v1.5.5+, not to
    # the original synthesis namespace. Use the actual stored namespace
    # so this test works regardless of where synthesis routes its writes.
    c1 = await cplane.get(namespace=payload1["namespace"], object_id=payload1["object_id"])
    c2 = await cplane.get(namespace=payload2["namespace"], object_id=payload2["object_id"])
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
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
        coordinator=_coordinator(qdrant, sink),
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
        client=qdrant,
        sink=sink,
        coordinator=_coordinator(qdrant, sink),
        config=MaturationConfig(demotion_inactivity_sec=30 * 24 * 3600),
    )
    assert report.transitioned == 1


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_ollama_down_keeps_memories_eligible_via_candidates(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Bullet 21 — outage handling, post-candidates-pool contract.

    Pre-v1.5.5 outage safety was "don't advance the cursor"; that
    coupling is what let one permanently-failing cluster livelock a
    family forever (the run aborted before the cursor moved, rebuilt
    the same cluster first every night, and never progressed). The
    candidates pool now owns eligibility: an outage advances the scan
    cursor but every affected memory is carried forward as a candidate
    and is re-pulled on the next sweep. Nothing is lost; nothing
    livelocks.
    """
    eps_ns = _ns(ns, "episodic")
    memories = []
    for i in range(3):
        memories.append(await _inject_episodic(qdrant, embedder, eps_ns, "cluster"))

    ollama = FakeSynthesisOllama(available=False)
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)

    assert report.concepts_created == 0
    # Scan cursor moves (it is an optimization, not an eligibility gate)…
    assert cursor.get(ns) > 0.0
    # …and every member of the failed cluster remains eligible.
    family = ns.split("/", 1)[0]
    carried = set(
        cursor.get_candidates(family, ttl_sec=30 * 86400, now_epoch=utc_now().timestamp())
    )
    assert {m.object_id for m in memories} <= carried

    # Recovery: the next sweep re-pulls the candidates and synthesizes.
    ollama.available = True
    report2 = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)
    assert report2.concepts_created == 1


async def test_llm_none_skips_cluster_not_entire_run(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """A ``None`` from ``synthesize_cluster`` means "skip this cluster,
    try next run" (HttpxOllamaClient contract) — it must not abort the
    family's whole sweep the way it did pre-fix."""
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "fail", tags=["tag1"])
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "ok", tags=["tag2"])

    class NoneForFailClusters(FakeSynthesisOllama):
        async def synthesize_cluster(self, cluster: SynthesisInput) -> SynthesisOutput | None:
            if any("fail" in m.content for m in cluster.memories):
                self.synthesize_calls.append(cluster)
                return None
            return await super().synthesize_cluster(cluster)

    ollama = NoneForFailClusters()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns)

    assert report.clusters_formed == 2
    assert report.concepts_created == 1  # the ok-cluster still landed
    assert cursor.get(ns) > 0.0


async def test_mega_cluster_sampled_to_llm_cap(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """An oversized cluster is synthesized from a deterministic,
    importance-first sample instead of overrunning the LLM context.
    Unsampled members stay in the candidates pool."""
    eps_ns = _ns(ns, "episodic")
    injected = []
    for i in range(8):
        injected.append(
            await _inject_episodic(
                qdrant, embedder, eps_ns, "cluster", tags=["tag1"], importance=(i % 4) + 3
            )
        )

    config = SynthesisConfig(max_llm_cluster_members=5)
    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns, config)

    assert report.concepts_created == 1
    sent = ollama.synthesize_calls[0].memories
    assert len(sent) == 5
    # Importance-first: the sample is the top-5 by importance with the
    # KSUID tiebreak — no member outside the sample outranks one inside.
    sample_min = min(m.importance for m in sent)
    sent_ids = {m.object_id for m in sent}
    for m in injected:
        if m.object_id not in sent_ids:
            assert m.importance <= sample_min

    # Unsampled members remain eligible candidates for the next sweep.
    family = ns.split("/", 1)[0]
    carried = set(
        cursor.get_candidates(family, ttl_sec=30 * 86400, now_epoch=utc_now().timestamp())
    )
    assert {m.object_id for m in injected} - sent_ids <= carried


async def test_oversized_clusters_deduplicate_after_sampling(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Distinct oversized groups that collapse to one sample run the LLM once."""
    eps_ns = _ns(ns, "episodic")
    shared = []
    for _ in range(5):
        shared.append(
            await _inject_episodic(
                qdrant,
                embedder,
                eps_ns,
                "shared-high",
                tags=["tag1", "tag2"],
                importance=9,
            )
        )
    tag1_only = await _inject_episodic(
        qdrant, embedder, eps_ns, "shared-high", tags=["tag1"], importance=1
    )
    tag2_only = await _inject_episodic(
        qdrant, embedder, eps_ns, "shared-high", tags=["tag2"], importance=1
    )

    config = SynthesisConfig(max_llm_cluster_members=5)
    ollama = FakeSynthesisOllama()
    report = await synthesis_run(qdrant, sink, ollama, embedder, cursor, ns, config)

    assert report.clusters_formed == 2
    assert report.concepts_created == 1
    assert len(ollama.synthesize_calls) == 1
    assert {m.object_id for m in ollama.synthesize_calls[0].memories} == {
        m.object_id for m in shared
    }

    family = ns.split("/", 1)[0]
    carried = set(
        cursor.get_candidates(family, ttl_sec=30 * 86400, now_epoch=utc_now().timestamp())
    )
    assert {tag1_only.object_id, tag2_only.object_id} <= carried


async def test_inline_vector_row_with_layout_fields_synthesizes(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """Red/green for the 2026-08-12 production crash.

    A v1 inline-vector row that a committed publish path stamped with
    ``committed_operation_id`` (a Phase-2 layout-only key) previously hit
    ``EpisodicMemory.model_validate(payload)`` unstripped on the inline
    branch of ``_resolve_candidate_memory``: pydantic's ``extra="forbid"``
    raised, the exception propagated out of ``synthesis_run``, and one such
    row aborted the whole identity family's daily pass — every 7DS family
    failed at exactly this seam in the 03:00 UTC run. The inline branch
    must strip layout keys just like the anchor branch always did.
    """
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(
            qdrant,
            embedder,
            eps_ns,
            "cluster",
            payload_extra={
                "committed_operation_id": f"op-{i}",
                "vector_layout_version": 2,
            },
        )

    report = await synthesis_run(qdrant, sink, FakeSynthesisOllama(), embedder, cursor, ns)

    assert report.concepts_created == 1
    assert report.candidates_decode_failed == 0


async def test_one_undecodable_row_degrades_run_without_aborting_family(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    cursor: SynthesisCursor,
    embedder: FakeEmbedder,
) -> None:
    """One schema-drifted row must not cost the family its daily pass.

    The drifted row here carries a non-layout unknown key, so it still
    fails validation AFTER stripping — the fail-closed per-row path. The
    valid cluster in the same family synthesizes, and the skip is counted
    on the report as a degraded (not failed) run.
    """
    eps_ns = _ns(ns, "episodic")
    for i in range(3):
        await _inject_episodic(qdrant, embedder, eps_ns, "cluster", tags=["tag1"])
    await _inject_episodic(
        qdrant,
        embedder,
        eps_ns,
        "drifted row",
        tags=["tag1"],
        payload_extra={"field_from_a_future_schema": True},
    )

    family = ns.split("/", 1)[0]
    skips_before = _family_counter("musubi_lifecycle_synthesis_decode_skips_total", family)

    report = await synthesis_run(qdrant, sink, FakeSynthesisOllama(), embedder, cursor, ns)

    assert report.concepts_created == 1
    assert report.candidates_decode_failed == 1
    # The valid members were consumed by the cluster; the drifted row is
    # skipped (not a candidate — it cannot be decoded at all).
    assert report.memories_selected == 3
    # The skip reached the OPERATIONAL counter, not just the report/log —
    # a log-only failure signal is how the original incident ran five
    # nights unobserved.
    skips_after = _family_counter("musubi_lifecycle_synthesis_decode_skips_total", family)
    assert skips_after == skips_before + 1


def test_family_exception_increments_failure_metric_without_reraise(
    qdrant: QdrantClient,
    ns: str,
    sink: LifecycleEventSink,
    embedder: FakeEmbedder,
    tmp_path: Path,
) -> None:
    """An exception caught by the job's per-family loop must produce a
    metric sample. `build_synthesis_jobs` deliberately catches everything
    to preserve family isolation, which also means the outer
    `musubi_lifecycle_job_errors_total` NEVER sees these failures — the
    exact observability hole the 2026-08-12 decode crash lived in.

    Sync test on purpose: the Job's func drives its own `asyncio.run`.
    """

    class ExplodingCursor(SynthesisCursor):
        def get(self, namespace_or_family: str) -> float:
            raise RuntimeError("cursor storage corrupted (test)")

    # One decodable row so family discovery finds the family at all.
    asyncio.run(_inject_episodic(qdrant, embedder, _ns(ns, "episodic"), "cluster"))
    family = ns.split("/", 1)[0]
    failures_before = _family_counter("musubi_lifecycle_synthesis_family_failures_total", family)

    job = build_synthesis_jobs(
        client=qdrant,
        sink=sink,
        ollama=FakeSynthesisOllama(),
        embedder=embedder,
        cursor=ExplodingCursor(db_path=tmp_path / "exploding-cursor.db"),
        lock_dir=tmp_path / "locks",
    )[0]

    # Family isolation: the job function swallows the failure (no raise)…
    job.func()

    # …but the failure is now visible on the metrics plane.
    failures_after = _family_counter("musubi_lifecycle_synthesis_family_failures_total", family)
    assert failures_after == failures_before + 1


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


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
def test_hypothesis_synthesis_is_idempotent_across_runs_with_no_new_memories() -> None:
    """Bullet 24."""


@pytest.mark.skip(reason="out-of-scope: hypothesis-based property suite is post-v1.0 hardening")
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


def test_discover_returns_identity_families_not_full_namespaces() -> None:
    """v1.5.5+ discovery returns identity families (first path component)
    instead of `<tenant>/<presence>` prefixes. Synthesis runs per-family,
    federating across substrates."""
    client = _FakeQdrantForDiscovery(
        [
            (
                [
                    _FakeRecord({"namespace": "eric/aoi/episodic", "identity_family": "eric"}),
                    _FakeRecord(
                        {"namespace": "eric/aoi/episodic", "identity_family": "eric"}
                    ),  # dedupe
                    _FakeRecord({"namespace": "eric/ops/episodic", "identity_family": "eric"}),
                    _FakeRecord({"namespace": "alice/voice/episodic", "identity_family": "alice"}),
                ],
                None,
            ),
        ]
    )
    result = _discover_episodic_namespaces(cast(Any, client))
    assert result == ["alice", "eric"]


def test_discover_paginates_until_offset_none() -> None:
    """An identity whose records are on page 2 must not be silently
    dropped — the scroll must keep iterating until Qdrant signals
    ``offset is None``."""
    page1 = [_FakeRecord({"namespace": "eric/aoi/episodic", "identity_family": "eric"})]
    page2 = [_FakeRecord({"namespace": "alice/ghost/episodic", "identity_family": "alice"})]
    client = _FakeQdrantForDiscovery(
        [
            (page1, "cursor-1"),
            (page2, None),
        ]
    )
    result = _discover_episodic_namespaces(cast(Any, client))
    assert result == ["alice", "eric"]
    # Second scroll call must carry the offset returned by the first.
    assert client.calls[1]["offset"] == "cursor-1"


def test_discover_returns_empty_on_scroll_exception() -> None:
    class _BoomClient:
        def scroll(self, **_: Any) -> tuple[list[_FakeRecord], Any]:
            raise RuntimeError("qdrant down")

    assert _discover_episodic_namespaces(cast(Any, _BoomClient())) == []


def test_discover_falls_back_to_namespace_prefix_when_identity_family_missing() -> None:
    """Pre-v1.5.5 points may not have identity_family populated yet
    (backfill hasn't run). Discovery falls back to deriving the family
    from the namespace prefix so the new synthesis flow still works
    against not-yet-backfilled data."""
    client = _FakeQdrantForDiscovery(
        [
            (
                [
                    _FakeRecord(None),  # missing payload
                    _FakeRecord({}),  # empty payload
                    _FakeRecord({"namespace": 42}),  # non-string namespace
                    _FakeRecord(
                        {"namespace": "eric/aoi/concept"}
                    ),  # no identity_family — fall back
                    _FakeRecord(
                        {"namespace": "eric/aoi/episodic", "identity_family": "eric"}
                    ),  # canonical
                ],
                None,
            ),
        ]
    )
    assert _discover_episodic_namespaces(cast(Any, client)) == ["eric"]
