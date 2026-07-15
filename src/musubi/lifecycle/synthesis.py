"""Daily job: cluster matured episodics and generate synthesized concepts.

Implements [[06-ingestion/concept-synthesis]].
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle import LifecycleEventSink, store
from musubi.lifecycle.scheduler import Job, file_lock
from musubi.planes.concept import ConceptPlane
from musubi.store.names import collection_for_plane
from musubi.store.specs import DENSE_VECTOR_NAME
from musubi.types.concept import SynthesizedConcept
from musubi.types.episodic import EpisodicMemory

logger = logging.getLogger(__name__)


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


class SynthesisCursor:
    """Tracks synthesis state across runs.

    Two coordinated stores:

    1. **Per-identity-family cursor** — high-water mark of "memories
       scanned so far." Used as a performance optimization to skip
       already-seen matured episodics during the new-memory pull.
       This is NOT a correctness gate — eligibility for clustering
       is determined by the candidates table, not by whether a
       memory's epoch is above the cursor.

    2. **Candidate pool** — per-(family, memory) state for "this
       memory has been seen but didn't successfully cluster yet."
       Carried forward across runs within a TTL window so slow-
       accumulating patterns can eventually form clusters when peer
       memories arrive in later runs. Successful clustering removes
       the candidate row; aging past TTL removes it without success.

    The pre-v1.5.5 `get(namespace)` / `set(namespace)` methods are
    retained for any callers that haven't migrated yet, but they now
    map to the family-keyed cursor underneath: namespaces of the form
    `<tenant>/<presence>` reduce to `<tenant>` for cursor lookup.
    """

    def __init__(
        self, *, db_path: Path, busy_timeout_ms: int = store.DEFAULT_BUSY_TIMEOUT_MS
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        with self._connect() as conn:
            store.ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        return store.connect(self._db_path, busy_timeout_ms=self._busy_timeout_ms)

    @staticmethod
    def _family_of(value: str) -> str:
        """Reduce a namespace-like input to the identity family.

        Thin wrapper around the public ``musubi.types.common.family_of``
        with the additional concession that bare-family inputs (no
        slash) pass through as-is. ``family_of`` itself raises on
        inputs without a separator because the public contract is
        "give me a namespace, get back the family"; this wrapper
        exists because cursor callers legitimately pass either form
        (e.g. the scheduler passes "aoi" while legacy callers pass
        "aoi/command-chair"). Centralising on the public helper for
        the namespace case keeps the two implementations from drifting.
        """
        if "/" not in value:
            return value
        from musubi.types.common import family_of

        return family_of(value)

    # ------------------------------------------------------------------
    # Cursor — high-water mark per identity family
    # ------------------------------------------------------------------

    def get(self, namespace_or_family: str) -> float:
        family = self._family_of(namespace_or_family)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_processed_epoch FROM synthesis_family_cursor "
                "WHERE identity_family = ?",
                (family,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def set(self, namespace_or_family: str, value: float) -> None:
        family = self._family_of(namespace_or_family)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO synthesis_family_cursor "
                "(identity_family, last_processed_epoch) VALUES (?, ?) "
                "ON CONFLICT(identity_family) DO UPDATE SET "
                "last_processed_epoch = excluded.last_processed_epoch",
                (family, value),
            )
            conn.commit()

    def reset(self, family: str | None = None) -> None:
        """Clear cursor state. Used by the backfill CLI to force a
        full re-synthesis pass across all matured memories."""
        with self._connect() as conn:
            if family is None:
                conn.execute("DELETE FROM synthesis_family_cursor")
            else:
                conn.execute(
                    "DELETE FROM synthesis_family_cursor WHERE identity_family = ?",
                    (family,),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Candidates — per-memory eligibility, not lost on cursor advance
    # ------------------------------------------------------------------

    def upsert_candidate(self, family: str, memory_object_id: str, *, now_epoch: float) -> None:
        """Mark a memory as a synthesis candidate; bump attempts on repeat.

        ``now_epoch`` is keyword-only for consistency with the sibling
        timing-aware methods (``get_candidates``, ``prune_aged_candidates``)
        and so call sites read self-documentingly.
        """
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO synthesis_candidates "
                "(identity_family, memory_object_id, first_seen_epoch, "
                "last_attempt_epoch, attempts) VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(identity_family, memory_object_id) DO UPDATE SET "
                "last_attempt_epoch = excluded.last_attempt_epoch, "
                "attempts = synthesis_candidates.attempts + 1",
                (family, memory_object_id, now_epoch, now_epoch),
            )
            conn.commit()

    def get_candidates(self, family: str, *, ttl_sec: float, now_epoch: float) -> list[str]:
        """Return memory object_ids currently within the candidate TTL window.

        Memories older than TTL are NOT returned here (they're filtered
        out as if they had been pruned, even if `prune_aged_candidates`
        hasn't run yet — defense against a worker restart between sweeps).
        """
        cutoff = now_epoch - ttl_sec
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT memory_object_id FROM synthesis_candidates "
                "WHERE identity_family = ? AND first_seen_epoch >= ?",
                (family, cutoff),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def remove_candidates(self, family: str, memory_object_ids: list[str]) -> None:
        """Remove successfully-clustered memories from the candidate pool."""
        if not memory_object_ids:
            return
        placeholders = ",".join("?" * len(memory_object_ids))
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM synthesis_candidates WHERE identity_family = ? "
                f"AND memory_object_id IN ({placeholders})",
                (family, *memory_object_ids),
            )
            conn.commit()

    def prune_aged_candidates(self, family: str, *, ttl_sec: float, now_epoch: float) -> int:
        """Delete candidate rows older than TTL. Returns number pruned."""
        cutoff = now_epoch - ttl_sec
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM synthesis_candidates "
                "WHERE identity_family = ? AND first_seen_epoch < ?",
                (family, cutoff),
            )
            conn.commit()
            return cursor.rowcount or 0

    def reset_candidates(self, family: str | None = None) -> None:
        """Clear the candidate pool. Used by backfill CLI."""
        with self._connect() as conn:
            if family is None:
                conn.execute("DELETE FROM synthesis_candidates")
            else:
                conn.execute(
                    "DELETE FROM synthesis_candidates WHERE identity_family = ?",
                    (family,),
                )
            conn.commit()


# ---------------------------------------------------------------------------
# Config + Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisConfig:
    """Parameters for the synthesis job.

    Defaults are the generous ones — same for every identity, no
    per-family stripping. The per-family override path exists for
    data-driven tuning later (auto-tune from each family's pairwise
    distribution) but ships empty in v1.5.5.
    """

    cluster_threshold: float = 0.70
    match_threshold: float = 0.85
    contradiction_min_similarity: float = 0.75
    contradiction_max_similarity: float = 0.85
    # min_cluster_size stays at 3 — the concept plane enforces a "min 3
    # source memories per concept" invariant (see _MIN_MERGED_FROM in
    # musubi.planes.concept.plane). A concept by definition is a pattern
    # across at least three observations, not two. Relaxing this would
    # weaken the meaning of "concept" downstream.
    min_cluster_size: int = 3
    # Candidates pool: memories that didn't cluster on their first pass
    # stay eligible for the next sweep until this many seconds elapse.
    # 30 days is a human-scale window — slow-accumulating patterns over
    # weeks still find each other; truly stranded memories eventually
    # age out instead of dragging on every sweep forever.
    candidate_ttl_sec: int = 30 * 86400


@dataclass(frozen=True)
class SynthesisReport:
    """Results of a single synthesis run.

    The `namespace` field carries the identity family in the v1.5.5+
    flow (e.g. "aoi"), not a full tenant/presence/plane namespace.
    Renamed semantically but kept its name for backward-compat with any
    log scrapers / dashboards that parse it.
    """

    namespace: str
    memories_selected: int
    clusters_formed: int
    concepts_created: int
    concepts_reinforced: int
    contradictions_detected: int
    cursor_advanced_to: float | None = None
    candidates_pruned: int = 0
    candidates_carried_forward: int = 0


# ---------------------------------------------------------------------------
# The Job
# ---------------------------------------------------------------------------

_FAMILY_SYNTHESIS_PRESENCE = "shared"
"""Convention: family-level synthesis writes concepts to
``<family>/shared/concept``. The `shared` presence already means
"identity-level, not substrate-specific" (see e.g. aoi/shared/episodic
which carries Aoi's joy entry, visual identity, codeword authority
— all identity-wide things)."""


async def synthesis_run(
    client: QdrantClient,
    sink: LifecycleEventSink,
    ollama: SynthesisOllamaClient,
    embedder: Embedder,
    cursor: SynthesisCursor,
    namespace: str,
    config: SynthesisConfig | None = None,
    *,
    now_epoch: float | None = None,
) -> SynthesisReport:
    """Run the synthesis loop for one identity family.

    The ``namespace`` parameter accepts either a bare identity family
    ("aoi") or a legacy namespace ("aoi/command-chair"); both reduce
    to the same family via the cursor helper. Synthesis scopes to
    every matured episodic with `identity_family=<family>`,
    regardless of which substrate captured it.

    The flow now uses the candidates pool to retain memories that
    didn't cluster on a previous pass — they remain eligible for
    `candidate_ttl_sec` (default 30 days), so slow-accumulating
    patterns can eventually form clusters once peer memories arrive.

    Concepts are written to ``<family>/shared/concept``.
    """
    cfg = config or SynthesisConfig()
    family = cursor._family_of(namespace)
    now = now_epoch if now_epoch is not None else time.time()
    cursor_val = cursor.get(family)
    memories_with_vectors: list[MemoryWithVector] = []
    seen_ids: set[str] = set()

    conc_ns = f"{family}/{_FAMILY_SYNTHESIS_PRESENCE}/concept"

    # Step 1a: Pull new matured memories (above the cursor high-water mark).
    offset: Any = None
    max_epoch = cursor_val
    while True:
        records, offset = client.scroll(
            collection_name=collection_for_plane("episodic"),
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="identity_family", match=models.MatchValue(value=family)
                    ),
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
                    seen_ids.add(memory.object_id)
                    if memory.updated_epoch and memory.updated_epoch > max_epoch:
                        max_epoch = memory.updated_epoch
        if offset is None:
            break

    # Step 1b: Pull unclustered candidates from prior sweeps within TTL.
    # Candidates are stored by object_id (KSUID), but Qdrant identifies
    # points by point_id (UUID5 derived from object_id) — translate via
    # the episodic plane's public `episodic_point_id` helper.
    from musubi.planes.episodic.plane import episodic_point_id

    candidate_ids = cursor.get_candidates(family, ttl_sec=cfg.candidate_ttl_sec, now_epoch=now)
    candidate_ids_to_fetch = [oid for oid in candidate_ids if oid not in seen_ids]
    if candidate_ids_to_fetch:
        retrieved = client.retrieve(
            collection_name=collection_for_plane("episodic"),
            ids=[episodic_point_id(oid) for oid in candidate_ids_to_fetch],
            with_payload=True,
            with_vectors=True,
        )
        for r in retrieved:
            if (
                r.payload
                and r.vector
                and isinstance(r.vector, dict)
                and DENSE_VECTOR_NAME in r.vector
            ):
                # Belt: only accept if still matured (a candidate could
                # have been demoted/archived since being marked).
                if r.payload.get("state") != "matured":
                    continue
                memory = EpisodicMemory.model_validate(r.payload)
                vector = r.vector[DENSE_VECTOR_NAME]
                if isinstance(vector, list):
                    memories_with_vectors.append(
                        MemoryWithVector(memory, cast(list[float], vector))
                    )
                    seen_ids.add(memory.object_id)

    if len(memories_with_vectors) < cfg.min_cluster_size:
        # Not enough memories to form even the smallest cluster. Every
        # one of them gets upserted as a candidate so it stays eligible
        # for the next sweep when more peers may arrive — both newly
        # scanned memories and existing candidates we just re-pulled
        # (the latter bumping their attempts counter).
        for mwv in memories_with_vectors:
            cursor.upsert_candidate(family, mwv.memory.object_id, now_epoch=now)
        if memories_with_vectors:
            cursor.set(family, max_epoch)
        pruned = cursor.prune_aged_candidates(family, ttl_sec=cfg.candidate_ttl_sec, now_epoch=now)
        return SynthesisReport(
            namespace=family,
            memories_selected=len(memories_with_vectors),
            clusters_formed=0,
            concepts_created=0,
            concepts_reinforced=0,
            contradictions_detected=0,
            cursor_advanced_to=max_epoch,
            candidates_pruned=pruned,
            candidates_carried_forward=len(memories_with_vectors),
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
    clustered_memory_ids: set[str] = set()

    for cluster_mwvs in clusters:
        cluster_memories = [mwv.memory for mwv in cluster_mwvs]
        try:
            output = await ollama.synthesize_cluster(SynthesisInput(cluster_memories))
            if not output:
                logger.error("LLM unavailable during synthesis_run, skipping run")
                # Don't advance the cursor or remove candidates on early
                # exit — let the next sweep retry from the same state.
                return SynthesisReport(
                    namespace=family,
                    memories_selected=len(memories_with_vectors),
                    clusters_formed=len(clusters),
                    concepts_created=concepts_created,
                    concepts_reinforced=concepts_reinforced,
                    contradictions_detected=contradictions_detected,
                    candidates_carried_forward=len(candidate_ids),
                )
        except Exception as e:
            logger.warning("Synthesis failed for cluster, skipping: %s", e)
            continue

        # Cluster successfully synthesized — mark its members as clustered.
        for m in cluster_memories:
            clustered_memory_ids.add(m.object_id)

        embed_text = f"{output.title}\n\n{output.rationale}"
        content_vector = (await embedder.embed_dense([embed_text]))[0]

        # Existing-match query federates: an old concept living in
        # `<family>/voice/concept` can be reinforced by a new cluster
        # discovered from `<family>/command-chair/episodic`, because
        # both share identity_family=<family>.
        existing_matches = client.query_points(
            collection_name=collection_for_plane("concept"),
            query=content_vector,
            using=DENSE_VECTOR_NAME,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="identity_family", match=models.MatchValue(value=family)
                    ),
                    models.FieldCondition(
                        key="state", match=models.MatchAny(any=["matured", "promoted"])
                    ),
                ]
            ),
            limit=1,
        ).points

        matched_concept_id = None
        matched_concept_ns = None
        if existing_matches and existing_matches[0].score >= cfg.match_threshold:
            payload = existing_matches[0].payload or {}
            matched_concept_id = cast(str | None, payload.get("object_id"))
            matched_concept_ns = cast(str | None, payload.get("namespace"))

        if matched_concept_id and matched_concept_ns:
            for memory in cluster_memories:
                await concept_plane.reinforce(
                    namespace=matched_concept_ns,
                    object_id=matched_concept_id,
                    additional_source=memory.object_id,
                )

            concepts_reinforced += 1
            refreshed = await concept_plane.get(
                namespace=matched_concept_ns, object_id=matched_concept_id
            )
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
                        curr = await concept_plane.get(
                            namespace=concept_a.namespace
                            if cid == concept_a.object_id
                            else concept_b.namespace,
                            object_id=cid,
                        )
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

    # Step 5: Candidates pool maintenance
    # - Remove members of successful clusters (they've done their job).
    # - Upsert any memory that DIDN'T cluster so it stays eligible
    #   for the next sweep until TTL expires.
    # - Prune candidates older than TTL.
    if clustered_memory_ids:
        cursor.remove_candidates(family, list(clustered_memory_ids))
    unclustered_ids = [
        mwv.memory.object_id
        for mwv in memories_with_vectors
        if mwv.memory.object_id not in clustered_memory_ids
    ]
    for oid in unclustered_ids:
        cursor.upsert_candidate(family, oid, now_epoch=now)
    pruned = cursor.prune_aged_candidates(family, ttl_sec=cfg.candidate_ttl_sec, now_epoch=now)

    # Step 6: Cursor (high-water mark only — eligibility is in candidates)
    cursor.set(family, max_epoch)

    return SynthesisReport(
        namespace=family,
        memories_selected=len(memories_with_vectors),
        clusters_formed=len(clusters),
        concepts_created=concepts_created,
        concepts_reinforced=concepts_reinforced,
        contradictions_detected=contradictions_detected,
        cursor_advanced_to=max_epoch,
        candidates_pruned=pruned,
        candidates_carried_forward=len(unclustered_ids),
    )


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def _discover_identity_families(client: QdrantClient, *, page_size: int = 1000) -> list[str]:
    """Enumerate identity families present in the episodic collection.

    Synthesis runs per-identity-family. Discovery at tick time keeps
    the runner from needing a re-deploy when a new identity shows up.
    Paginates until Qdrant returns ``offset=None`` so a new identity
    whose records don't fall in the first page can't be silently
    dropped.

    Prefers reading the indexed ``identity_family`` payload field
    directly; falls back to deriving from ``namespace`` for pre-v1.5.5
    points that haven't been backfilled yet.
    """
    discovered: set[str] = set()
    offset: Any = None
    while True:
        try:
            records, offset = client.scroll(
                collection_name=collection_for_plane("episodic"),
                limit=page_size,
                offset=offset,
                with_payload=["namespace", "identity_family"],
                with_vectors=False,
            )
        except Exception:
            logger.exception("synthesis-family-discovery-failed")
            return []
        for rec in records:
            if not rec.payload:
                continue
            fam = rec.payload.get("identity_family")
            if isinstance(fam, str) and fam:
                discovered.add(fam)
                continue
            # Fall back to namespace prefix for un-backfilled points.
            ns = rec.payload.get("namespace")
            if isinstance(ns, str) and "/" in ns:
                discovered.add(ns.split("/", 1)[0])
        if offset is None:
            break
    return sorted(discovered)


# Back-compat alias for any caller importing the old name. New code uses
# _discover_identity_families directly.
_discover_episodic_namespaces = _discover_identity_families


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
        families = _discover_identity_families(client)
        if not families:
            logger.info("synthesis-no-families-found")
            return
        for family in families:
            try:
                report = await synthesis_run(
                    client=client,
                    sink=sink,
                    ollama=ollama,
                    embedder=embedder,
                    cursor=cursor,
                    namespace=family,  # family identifier; legacy param name
                    config=config,
                )
                logger.info(
                    "synthesis-done family=%s selected=%d clusters=%d created=%d "
                    "reinforced=%d contradictions=%d candidates_carried=%d pruned=%d",
                    report.namespace,
                    report.memories_selected,
                    report.clusters_formed,
                    report.concepts_created,
                    report.concepts_reinforced,
                    report.contradictions_detected,
                    report.candidates_carried_forward,
                    report.candidates_pruned,
                )
            except Exception:
                logger.exception("synthesis-failed family=%s", family)

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
