"""Daily job: cluster matured episodics and generate synthesized concepts.

Implements [[06-ingestion/concept-synthesis]].
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Protocol, cast

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle import LifecycleEventSink
from musubi.lifecycle.scheduler import Job, file_lock
from musubi.observability import default_registry
from musubi.planes.concept import ConceptPlane
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME
from musubi.types.concept import SynthesizedConcept
from musubi.types.episodic import EpisodicMemory

logger = logging.getLogger(__name__)

_REG = default_registry()
_DURATION = _REG.histogram(
    "musubi_lifecycle_job_duration_seconds",
    "lifecycle worker tick duration",
    labelnames=("job",),
)
_ERRORS = _REG.counter(
    "musubi_lifecycle_job_errors_total",
    "lifecycle worker tick errors",
    labelnames=("job",),
)


def _instrument_synthesis_job[**P, R](
    func: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    @wraps(func)
    async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.monotonic()
        try:
            return await func(*args, **kwargs)
        except Exception:
            _ERRORS.labels(job="synthesis").inc()
            raise
        finally:
            _DURATION.labels(job="synthesis").observe(time.monotonic() - start)

    return _wrapped


# ---------------------------------------------------------------------------
# LLM Interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisInput:
    """A cluster of memories to be synthesized into a concept."""

    memories: list[EpisodicMemory]


@dataclass(frozen=True)
class SynthesisOutput:
    """LLM's response for a cluster synthesis."""

    title: str
    content: str
    rationale: str
    tags: list[str]
    importance: int
    contradicts_notice: str = ""


@dataclass(frozen=True)
class ContradictionInput:
    """A pair of concepts to check for contradiction."""

    concept_a: SynthesizedConcept
    concept_b: SynthesizedConcept


@dataclass(frozen=True)
class ContradictionOutput:
    """LLM's response for a contradiction check."""

    verdict: str  # "consistent" | "contradictory"
    reason: str


class SynthesisOllamaClient(Protocol):
    """Protocol for LLM interactions in the synthesis job."""

    async def synthesize_cluster(self, cluster: SynthesisInput) -> SynthesisOutput | None:
        """Ask LLM to find a common theme for a cluster of memories.

        Returns None if Ollama is unavailable.
        """
        ...

    async def check_contradiction(self, pair: ContradictionInput) -> ContradictionOutput | None:
        """Ask LLM if two concepts are consistent or contradictory.

        Returns None if Ollama is unavailable.
        """
        ...


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


@dataclass
class MemoryWithVector:
    memory: EpisodicMemory
    vector: list[float]


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2, strict=True))
    mag1 = sum(a * a for a in v1) ** 0.5
    mag2 = sum(a * a for a in v2) ** 0.5
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return float(dot / (mag1 * mag2))


def _threshold_cluster(
    items: list[MemoryWithVector], threshold: float
) -> list[list[MemoryWithVector]]:
    if not items:
        return []
    n = len(items)
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cosine_similarity(items[i].vector, items[j].vector)
            if sim >= threshold:
                adj[i].append(j)
                adj[j].append(i)

    visited = [False] * n
    clusters = []
    for i in range(n):
        if not visited[i]:
            cluster = []
            stack = [i]
            visited[i] = True
            while stack:
                u = stack.pop()
                cluster.append(items[u])
                for v in adj[u]:
                    if not visited[v]:
                        visited[v] = True
                        stack.append(v)
            clusters.append(cluster)
    return clusters


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS synthesis_cursor (
    namespace TEXT PRIMARY KEY,
    last_processed_epoch REAL NOT NULL
);
"""


class SynthesisCursor:
    """Tracks the last run per-namespace to avoid re-processing matured episodics."""

    def __init__(self, *, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CURSOR_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    def get(self, namespace: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_processed_epoch FROM synthesis_cursor WHERE namespace = ?",
                (namespace,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set(self, namespace: str, value: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO synthesis_cursor (namespace, last_processed_epoch) "
                "VALUES (?, ?) ON CONFLICT(namespace) DO UPDATE SET "
                "last_processed_epoch = excluded.last_processed_epoch",
                (namespace, value),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Config + Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisConfig:
    """Parameters for the synthesis job."""

    cluster_threshold: float = 0.80
    match_threshold: float = 0.85
    contradiction_min_similarity: float = 0.75
    contradiction_max_similarity: float = 0.85
    min_cluster_size: int = 3


@dataclass(frozen=True)
class SynthesisReport:
    """Results of a single synthesis run."""

    namespace: str
    memories_selected: int
    clusters_formed: int
    concepts_created: int
    concepts_reinforced: int
    contradictions_detected: int
    cursor_advanced_to: float | None = None


# ---------------------------------------------------------------------------
# The Job
# ---------------------------------------------------------------------------


@_instrument_synthesis_job
async def synthesis_run(
    client: QdrantClient,
    sink: LifecycleEventSink,
    ollama: SynthesisOllamaClient,
    embedder: Embedder,
    cursor: SynthesisCursor,
    namespace: str,
    config: SynthesisConfig | None = None,
) -> SynthesisReport:
    """Run the synthesis loop for a single namespace."""
    cfg = config or SynthesisConfig()
    cursor_val = cursor.get(namespace)
    memories_with_vectors: list[MemoryWithVector] = []

    eps_ns = f"{namespace}/episodic"
    conc_ns = f"{namespace}/concept"

    # Step 1: Selection
    offset: Any = None
    max_epoch = cursor_val
    while True:
        records, offset = client.scroll(
            collection_name=collection_for_plane("episodic"),
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=eps_ns)),
                    models.FieldCondition(key="state", match=models.MatchValue(value="matured")),
                    models.FieldCondition(key="updated_epoch", range=models.Range(gt=cursor_val)),
                ]
            ),
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for r in records:
            if (
                r.payload
                and r.vector
                and isinstance(r.vector, dict)
                and DENSE_VECTOR_NAME in r.vector
            ):
                memory = EpisodicMemory.model_validate(r.payload)
                vector = r.vector[DENSE_VECTOR_NAME]
                if isinstance(vector, list):
                    memories_with_vectors.append(
                        MemoryWithVector(memory, cast(list[float], vector))
                    )
                    if memory.updated_epoch and memory.updated_epoch > max_epoch:
                        max_epoch = memory.updated_epoch
        if offset is None:
            break

    if len(memories_with_vectors) < cfg.min_cluster_size:
        if memories_with_vectors:
            cursor.set(namespace, max_epoch)
        return SynthesisReport(
            namespace, len(memories_with_vectors), 0, 0, 0, 0, cursor_advanced_to=max_epoch
        )

    # Step 2: Clustering
    tag_groups = defaultdict(list)
    for mwv in memories_with_vectors:
        keys = mwv.memory.linked_to_topics or mwv.memory.tags[:2]
        if not keys:
            keys = ["_no_tags_"]
        for k in keys:
            tag_groups[k].append(mwv)

    clusters: list[list[MemoryWithVector]] = []
    seen_cluster_fingerprints = set()

    for group_mwvs in tag_groups.values():
        group_clusters = _threshold_cluster(group_mwvs, cfg.cluster_threshold)
        for c in group_clusters:
            if len(c) >= cfg.min_cluster_size:
                fingerprint = frozenset(m.memory.object_id for m in c)
                if fingerprint not in seen_cluster_fingerprints:
                    clusters.append(c)
                    seen_cluster_fingerprints.add(fingerprint)

    # Step 3: Concept generation
    concept_plane = ConceptPlane(client=client, embedder=embedder)
    concepts_created = 0
    concepts_reinforced = 0
    contradictions_detected = 0
    current_run_concepts: list[SynthesizedConcept] = []

    for cluster_mwvs in clusters:
        cluster_memories = [mwv.memory for mwv in cluster_mwvs]
        try:
            output = await ollama.synthesize_cluster(SynthesisInput(cluster_memories))
            if not output:
                logger.error("LLM unavailable during synthesis_run, skipping run")
                return SynthesisReport(
                    namespace,
                    len(memories_with_vectors),
                    len(clusters),
                    concepts_created,
                    concepts_reinforced,
                    contradictions_detected,
                )
        except Exception as e:
            logger.warning("Synthesis failed for cluster, skipping: %s", e)
            continue

        embed_text = f"{output.title}\n\n{output.rationale}"
        content_vector = (await embedder.embed_dense([embed_text]))[0]

        existing_matches = client.query_points(
            collection_name=collection_for_plane("concept"),
            query=content_vector,
            using=DENSE_VECTOR_NAME,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(key="namespace", match=models.MatchValue(value=conc_ns)),
                    models.FieldCondition(
                        key="state", match=models.MatchAny(any=["matured", "promoted"])
                    ),
                ]
            ),
            limit=1,
        ).points

        matched_concept_id = None
        if existing_matches and existing_matches[0].score >= cfg.match_threshold:
            matched_concept_id = (
                cast(str, existing_matches[0].payload["object_id"])
                if existing_matches[0].payload
                else None
            )

        if matched_concept_id:
            for memory in cluster_memories:
                await concept_plane.reinforce(
                    namespace=conc_ns,
                    object_id=matched_concept_id,
                    additional_source=memory.object_id,
                )

            concepts_reinforced += 1
            refreshed = await concept_plane.get(namespace=conc_ns, object_id=matched_concept_id)
            if refreshed:
                current_run_concepts.append(refreshed)
        else:
            new_concept = SynthesizedConcept(
                namespace=conc_ns,
                title=output.title,
                content=output.content,
                synthesis_rationale=output.rationale,
                tags=output.tags,
                importance=output.importance,
                merged_from=[m.object_id for m in cluster_memories],
            )
            created = await concept_plane.create(new_concept)
            concepts_created += 1
            current_run_concepts.append(created)

    # Step 4: Contradiction detection
    for i in range(len(current_run_concepts)):
        for j in range(i + 1, len(current_run_concepts)):
            concept_a = current_run_concepts[i]
            concept_b = current_run_concepts[j]

            vec_a = (await embedder.embed_dense([concept_a.content]))[0]
            vec_b = (await embedder.embed_dense([concept_b.content]))[0]
            sim = _cosine_similarity(vec_a, vec_b)

            if cfg.contradiction_min_similarity <= sim < cfg.contradiction_max_similarity:
                verdict = await ollama.check_contradiction(ContradictionInput(concept_a, concept_b))
                if verdict and verdict.verdict == "contradictory":
                    for cid, other_id in [
                        (concept_a.object_id, concept_b.object_id),
                        (concept_b.object_id, concept_a.object_id),
                    ]:
                        curr = await concept_plane.get(namespace=conc_ns, object_id=cid)
                        if curr:
                            new_contradicts = list({*curr.contradicts, other_id})
                            client.set_payload(
                                collection_name=collection_for_plane("concept"),
                                payload={"contradicts": new_contradicts},
                                points=models.Filter(
                                    must=[
                                        models.FieldCondition(
                                            key="object_id", match=models.MatchValue(value=cid)
                                        )
                                    ]
                                ),
                            )
                    contradictions_detected += 1

    # Step 6: Cursor
    cursor.set(namespace, max_epoch)

    return SynthesisReport(
        namespace=namespace,
        memories_selected=len(memories_with_vectors),
        clusters_formed=len(clusters),
        concepts_created=concepts_created,
        concepts_reinforced=concepts_reinforced,
        contradictions_detected=contradictions_detected,
        cursor_advanced_to=max_epoch,
    )


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def _discover_episodic_namespaces(client: QdrantClient, *, page_size: int = 1000) -> list[str]:
    """Enumerate `<tenant>/<presence>` prefixes currently present in the
    episodic collection.

    The synthesis sweep runs per-namespace; discovery at tick time keeps
    the runner from needing a re-deploy when a new user / presence shows
    up. Paginates until Qdrant returns ``offset=None`` so a new
    namespace whose records don't fall in the first page can't be
    silently dropped. ``page_size`` tunes memory pressure, not the
    ceiling of namespaces discoverable.
    """
    discovered: set[str] = set()
    offset: Any = None
    while True:
        try:
            records, offset = client.scroll(
                collection_name=collection_for_plane("episodic"),
                limit=page_size,
                offset=offset,
                with_payload=["namespace"],
                with_vectors=False,
            )
        except Exception:
            logger.exception("synthesis-ns-discovery-failed")
            return []
        for rec in records:
            if not rec.payload:
                continue
            full = rec.payload.get("namespace")
            if not isinstance(full, str):
                continue
            # Per-plane suffix convention: `<tenant>/<presence>/<plane>`.
            # Strip the trailing `/episodic` to get the synthesis-run form.
            if full.endswith("/episodic"):
                discovered.add(full[: -len("/episodic")])
        if offset is None:
            break
    return sorted(discovered)


def build_synthesis_jobs(
    *,
    client: QdrantClient,
    sink: LifecycleEventSink,
    ollama: SynthesisOllamaClient,
    embedder: Embedder,
    cursor: SynthesisCursor,
    lock_dir: Path,
    config: SynthesisConfig | None = None,
) -> list[Job]:
    """Return the one-element Job list matching
    :func:`musubi.lifecycle.scheduler.build_default_jobs`'s
    ``synthesis`` entry (daily at 03:00 UTC).

    The wrapped runner discovers namespaces via
    :func:`_discover_episodic_namespaces` each tick and calls
    :func:`synthesis_run` once per namespace. A file lock on
    ``lock_dir/synthesis.lock`` serialises against any other worker
    attempting the same sweep.
    """
    import asyncio as _asyncio

    lock_path = lock_dir / "synthesis.lock"

    async def _run_all() -> None:
        namespaces = _discover_episodic_namespaces(client)
        if not namespaces:
            logger.info("synthesis-no-namespaces-found")
            return
        for ns in namespaces:
            try:
                report = await synthesis_run(
                    client=client,
                    sink=sink,
                    ollama=ollama,
                    embedder=embedder,
                    cursor=cursor,
                    namespace=ns,
                    config=config,
                )
                logger.info(
                    "synthesis-done ns=%s selected=%d clusters=%d created=%d reinforced=%d contradictions=%d",
                    ns,
                    report.memories_selected,
                    report.clusters_formed,
                    report.concepts_created,
                    report.concepts_reinforced,
                    report.contradictions_detected,
                )
            except Exception:
                logger.exception("synthesis-failed ns=%s", ns)

    def _runner() -> None:
        with file_lock(lock_path) as acquired:
            if not acquired:
                logger.info("lifecycle-job=synthesis lock-held; skipping run")
                return
            _asyncio.run(_run_all())

    return [
        Job(
            name="synthesis",
            trigger_kind="cron",
            trigger_kwargs={"hour": 3, "minute": 0},
            func=_runner,
            grace_time_s=3600,
        ),
    ]


__all__ = [
    "ContradictionInput",
    "ContradictionOutput",
    "SynthesisConfig",
    "SynthesisCursor",
    "SynthesisInput",
    "SynthesisOllamaClient",
    "SynthesisOutput",
    "SynthesisReport",
    "build_synthesis_jobs",
    "synthesis_run",
]
