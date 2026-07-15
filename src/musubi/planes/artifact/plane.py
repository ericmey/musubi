"""``ArtifactPlane`` — CRUD + chunking for :class:`SourceArtifact`.

Manages the `musubi_artifact` (metadata) and `musubi_artifact_chunks`
(searchable content) collections.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator, TransitionPending
from musubi.lifecycle.transitions import TransitionError, TransitionResult, transition
from musubi.planes.artifact.chunking import get_chunker
from musubi.store.raw_lookup import point_exists, raw_payload
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.artifact import ArtifactChunk, SourceArtifact
from musubi.types.common import (
    KSUID,
    Err,
    LifecycleState,
    Namespace,
    Result,
    epoch_of,
    generate_ksuid,
    utc_now,
)

# Plane-specific point namespace UUID
_POINT_NS = uuid.UUID("6b0d5e2e-1e8e-4e0f-8e3e-000000000004")


def _point_id(object_id: str) -> str:
    return str(uuid.uuid5(_POINT_NS, object_id))


def _sparse_to_model(sparse: dict[int, float]) -> models.SparseVector:
    return models.SparseVector(
        indices=list(sparse.keys()),
        values=list(sparse.values()),
    )


def _artifact_from_payload(payload: dict[str, Any]) -> SourceArtifact:
    return SourceArtifact.model_validate(payload)


def _chunk_from_payload(payload: dict[str, Any]) -> ArtifactChunk:
    # Strip fields added for Qdrant filtering that aren't in ArtifactChunk model
    filtered = {
        k: v
        for k, v in payload.items()
        if k not in {"namespace", "content_type", "chunker", "source_system", "created_epoch"}
    }
    return ArtifactChunk.model_validate(filtered)


class ArtifactPlane:
    def __init__(self, *, client: QdrantClient, embedder: Embedder) -> None:
        self._client = client
        self._embedder = embedder
        self._collection = "musubi_artifact"
        self._chunks_collection = "musubi_artifact_chunks"

    async def create(self, artifact: SourceArtifact) -> SourceArtifact:
        """Insert artifact metadata point. Append-only."""
        existing = await self.get(namespace=artifact.namespace, object_id=artifact.object_id)
        if existing is not None:
            raise ValueError(f"artifact {artifact.object_id!r} already exists")

        zero_dense = (await self._embedder.embed_dense([" "]))[0]
        zero_dense = [0.0] * len(zero_dense)

        point = models.PointStruct(
            id=_point_id(artifact.object_id),
            payload=artifact.model_dump(mode="json"),
            # Zero vector for metadata-only point
            vector={
                DENSE_VECTOR_NAME: zero_dense,
            },
        )
        self._client.upsert(collection_name=self._collection, points=[point])
        return artifact

    async def index(self, artifact: SourceArtifact, content: str) -> SourceArtifact:
        """Synchronous COMMITTED index (C4/ART-001): chunk + embed, stage chunks tagged with a
        never-reused ``(generation, owner_token)``, then publish the head naming that committed pair.
        Reads are fail-closed on the head, so a re-index atomically switches the visible generation and
        hides the prior tail — the orphaned-chunk bug is gone on the direct-call path too. The async,
        durably-retried, concurrency-safe path is :class:`ArtifactIndexer` via the lifecycle worker;
        this method is the single-writer synchronous equivalent."""
        chunker = get_chunker(artifact.chunker)
        prior_generation = (
            artifact.committed_generation
        )  # generation this re-index supersedes (or None)
        staged_generation: str | None = None
        try:
            raw_chunks = chunker.chunk(content)
            if not raw_chunks:
                raise ValueError("chunking produced no chunks")

            generation = secrets.token_hex(16)  # never reused (the ABA fence, with owner)
            owner = secrets.token_hex(16)
            texts = [c.content for c in raw_chunks]
            dense_batch = await self._embedder.embed_dense(texts)
            sparse_batch = await self._embedder.embed_sparse(texts)

            points = []
            for i, rc in enumerate(raw_chunks):
                chunk_id = generate_ksuid()
                chunk = ArtifactChunk(
                    chunk_id=chunk_id,
                    artifact_id=artifact.object_id,
                    chunk_index=rc.index,
                    content=rc.content,
                    start_offset=rc.start_offset,
                    end_offset=rc.end_offset,
                    chunk_metadata=rc.metadata,
                    generation=generation,
                    owner_token=owner,
                )
                payload = chunk.model_dump(mode="json")
                payload["namespace"] = artifact.namespace
                payload["content_type"] = artifact.content_type
                payload["chunker"] = artifact.chunker
                payload["created_epoch"] = artifact.created_epoch
                points.append(
                    models.PointStruct(
                        id=_point_id(chunk_id),
                        payload=payload,
                        vector={
                            DENSE_VECTOR_NAME: dense_batch[i],
                            SPARSE_VECTOR_NAME: _sparse_to_model(sparse_batch[i]),
                        },
                    )
                )
            self._client.upsert(collection_name=self._chunks_collection, points=points)
            staged_generation = generation

            # Publish the committed head naming this generation+owner.
            now = utc_now()
            data = artifact.model_dump()
            data.update(
                artifact_state="indexed",
                chunk_count=len(raw_chunks),
                committed_generation=generation,
                committed_owner=owner,
                publication_version=artifact.publication_version + 1,
                failure_reason=None,
                updated_at=now,
                updated_epoch=epoch_of(now),
            )
            updated = SourceArtifact.model_validate(data)
            self._client.set_payload(
                collection_name=self._collection,
                payload=updated.model_dump(mode="json"),
                points=[_point_id(artifact.object_id)],
            )
            # Scoped GC: remove ONLY the SUPERSEDED prior generation's chunks — never a concurrent
            # attempt's fresh generation. Storage-only; reads are already fail-closed on the head.
            if prior_generation and prior_generation != generation:
                self._delete_generation(artifact.object_id, prior_generation)
            return updated

        except Exception as e:
            now = utc_now()
            data = artifact.model_dump()
            if prior_generation:
                # RE-INDEX failure (inv #3): the PREVIOUS-GOOD committed generation stays VISIBLE.
                # Record the failed attempt on the head but keep it indexed at the prior generation.
                data.update(failure_reason=str(e), updated_at=now, updated_epoch=epoch_of(now))
            else:
                # FIRST-EVER index failure (inv #4): fail-closed — no committed generation, zero exposed.
                data.update(
                    artifact_state="failed",
                    failure_reason=str(e),
                    committed_generation=None,
                    committed_owner=None,
                    updated_at=now,
                    updated_epoch=epoch_of(now),
                )
            result = SourceArtifact.model_validate(data)
            self._client.set_payload(
                collection_name=self._collection,
                payload=result.model_dump(mode="json"),
                points=[_point_id(artifact.object_id)],
            )
            # Scoped GC: remove ONLY this failed attempt's own staged generation (if it staged any).
            if staged_generation is not None:
                self._delete_generation(artifact.object_id, staged_generation)
            return result

    async def mark_index_unadmitted(self, artifact: SourceArtifact) -> SourceArtifact:
        """Record a VISIBLE terminal disposition when an indexing intent could not be admitted (the
        lifecycle outbox is at capacity): mark the head ``failed`` with a backpressure reason and NO
        committed generation (fail-closed). The artifact is a visible ``failed``, never silently stuck
        ``indexing``; re-upload to retry."""
        now = utc_now()
        data = artifact.model_dump()
        data.update(
            artifact_state="failed",
            failure_reason="indexing not admitted: lifecycle outbox at capacity; re-upload to retry",
            committed_generation=None,
            committed_owner=None,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        failed = SourceArtifact.model_validate(data)
        self._client.set_payload(
            collection_name=self._collection,
            payload=failed.model_dump(mode="json"),
            points=[_point_id(artifact.object_id)],
        )
        return failed

    def _delete_generation(self, object_id: KSUID, generation: str) -> None:
        """Delete every chunk of one artifact bearing a SPECIFIC generation — scoped storage-only
        cleanup that never touches another generation (a concurrent attempt's fresh one, or the
        committed one)."""
        self._client.delete(
            collection_name=self._chunks_collection,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="artifact_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="generation", match=models.MatchValue(value=generation)
                    ),
                ]
            ),
        )

    async def exists(self, *, namespace: Namespace, object_id: KSUID) -> bool:
        """Is this row present? Answered WITHOUT deserializing it.

        ``get()`` model-validates, so it raises on a corrupted row — which meant any
        caller using it merely to ask "is it there?" inherited a hard failure on
        exactly the rows that are broken, and a corrupted row could not be deleted or
        archived. The removability of a memory must never depend on that memory being
        valid. See :mod:`musubi.store.raw_lookup`.
        """
        return point_exists(
            self._client, self._collection, namespace=namespace, object_id=object_id
        )

    async def raw_payload(self, *, namespace: Namespace, object_id: KSUID) -> dict[str, Any] | None:
        """The stored payload exactly as persisted — never model-validated.

        The inspection/repair door for a row the model refuses to open. Treat every key
        as untrusted: ``.get()`` with a default, never index.
        """
        return raw_payload(self._client, self._collection, namespace=namespace, object_id=object_id)

    async def get(self, *, namespace: Namespace, object_id: KSUID) -> SourceArtifact | None:
        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if not records:
            return None
        payload = records[0].payload
        if not payload:
            return None
        return _artifact_from_payload(payload)

    def _get_sync(self, *, namespace: Namespace, object_id: KSUID) -> SourceArtifact | None:
        """Synchronous head read (``get`` has an async signature but a fully synchronous body); used
        inside the fail-closed read filter."""
        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if not records or not records[0].payload:
            return None
        return _artifact_from_payload(records[0].payload)

    def _committed_pair(
        self,
        chunk: ArtifactChunk,
        cache: dict[str, tuple[str | None, str | None]],
        namespace: Namespace,
    ) -> bool:
        """C4 fail-closed visibility: is this chunk part of its artifact's CURRENT committed
        generation? Resolve the head (cached), then require a non-None committed generation+owner
        that EXACTLY equals the chunk's. A legacy/uncommitted head (committed_generation is None)
        exposes ZERO chunks — a generation-less legacy chunk never matches a real generation."""
        if chunk.artifact_id not in cache:
            head = self._get_sync(namespace=namespace, object_id=chunk.artifact_id)
            cache[chunk.artifact_id] = (
                (head.committed_generation, head.committed_owner)
                if head is not None
                else (None, None)
            )
        cg, co = cache[chunk.artifact_id]
        return bool(cg) and bool(co) and chunk.generation == cg and chunk.owner_token == co

    async def _committed_query(
        self, *, namespace: Namespace, query: str, limit: int, budget: int
    ) -> tuple[list[ArtifactChunk], int]:
        """Over-fetch up to ``budget`` candidate chunks and expose only those whose ``(generation,
        owner_token)`` match their artifact's CURRENT committed head. Returns ``(committed, seen)`` —
        ``seen`` is the candidate count, used to tell a genuinely-sparse result from a budget-truncated
        one (publication churn)."""
        dense = (await self._embedder.embed_dense([query]))[0]
        resp = self._client.query_points(
            collection_name=self._chunks_collection,
            query=dense,
            using=DENSE_VECTOR_NAME,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                ]
            ),
            limit=budget,
            with_payload=True,
        )
        candidates = [p for p in resp.points if p.payload]
        cache: dict[str, tuple[str | None, str | None]] = {}
        out: list[ArtifactChunk] = []
        for point in candidates:
            assert point.payload is not None
            chunk = _chunk_from_payload(point.payload)
            if self._committed_pair(chunk, cache, namespace):
                out.append(chunk)
                if len(out) >= limit:
                    break
        return out, len(candidates)

    async def query(
        self,
        *,
        namespace: Namespace,
        query: str,
        limit: int = 10,
    ) -> list[ArtifactChunk]:
        """Head-first, FAIL-CLOSED semantic retrieval: over-fetch candidates, then expose only chunks
        whose ``(generation, owner_token)`` equal their artifact's CURRENT committed head. No staged,
        failed, or non-current-generation chunk is ever returned. This variant returns the committed
        list only; for the explicit bounded-partial + ``generation_churn`` degradation contract use
        :meth:`query_with_degradation`."""
        out, _ = await self._committed_query(
            namespace=namespace, query=query, limit=limit, budget=max(limit * 4, 40)
        )
        return out

    async def query_with_degradation(
        self,
        *,
        namespace: Namespace,
        query: str,
        limit: int = 10,
    ) -> tuple[list[ArtifactChunk], list[str]]:
        """Head-first, fail-closed retrieval with the accepted C4 GLOBAL-SEARCH contract: over-fetch a
        candidate budget, expose only committed chunks, and — if the budget CEILING was hit while still
        under-filling ``limit`` (publication churn may hide committed chunks beyond the budget) — retry
        ONCE with a larger budget, then return the bounded PARTIAL result plus an explicit
        ``['generation_churn']`` warning rather than silently claiming completeness. A genuinely sparse
        result (fewer than ``limit`` committed chunks exist, budget not exhausted) returns no warning."""
        budget = max(limit * 4, 40)
        out, seen = await self._committed_query(
            namespace=namespace, query=query, limit=limit, budget=budget
        )
        warnings: list[str] = []
        if len(out) < limit and seen >= budget:
            out, seen = await self._committed_query(
                namespace=namespace, query=query, limit=limit, budget=budget * 2
            )
            if len(out) < limit and seen >= budget * 2:
                warnings.append("generation_churn")
        return out, warnings

    async def chunks_for(self, *, namespace: Namespace, object_id: KSUID) -> list[ArtifactChunk]:
        """The COMMITTED chunks of one artifact, ordered by index: resolve the head, then return only
        chunks whose ``(generation, owner_token)`` equal the head's committed pair. Fail-closed — a
        head with no committed generation (legacy / indexing / failed) exposes ZERO chunks."""
        head = await self.get(namespace=namespace, object_id=object_id)
        if head is None or not head.committed_generation or not head.committed_owner:
            return []
        records, _ = self._client.scroll(
            collection_name=self._chunks_collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="artifact_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="generation", match=models.MatchValue(value=head.committed_generation)
                    ),
                    models.FieldCondition(
                        key="owner_token", match=models.MatchValue(value=head.committed_owner)
                    ),
                ]
            ),
            limit=10000,
            with_payload=True,
        )
        chunks = [_chunk_from_payload(r.payload) for r in records if r.payload]
        chunks.sort(key=lambda c: c.chunk_index)
        return chunks

    async def query_by_artifact(self, *, artifact_id: KSUID) -> list[ArtifactChunk]:
        """The COMMITTED chunks for an artifact_id, ordered by index — head-first, FAIL-CLOSED.
        Resolves the head by ``object_id`` (KSUID is globally unique, so no namespace is needed) and
        returns only chunks whose ``(generation, owner_token)`` equal the head's committed pair. A
        legacy / uncommitted / failed head (no committed generation) exposes ZERO chunks."""
        head_records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=artifact_id)
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if not head_records or not head_records[0].payload:
            return []
        head = _artifact_from_payload(head_records[0].payload)
        if not head.committed_generation or not head.committed_owner:
            return []
        records, _ = self._client.scroll(
            collection_name=self._chunks_collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="artifact_id", match=models.MatchValue(value=artifact_id)
                    ),
                    models.FieldCondition(
                        key="generation", match=models.MatchValue(value=head.committed_generation)
                    ),
                    models.FieldCondition(
                        key="owner_token", match=models.MatchValue(value=head.committed_owner)
                    ),
                ]
            ),
            limit=10000,
            with_payload=True,
        )
        chunks = [_chunk_from_payload(r.payload) for r in records if r.payload]
        chunks.sort(key=lambda c: c.chunk_index)
        return chunks

    async def transition(
        self,
        *,
        namespace: Namespace,
        object_id: KSUID,
        to_state: LifecycleState,
        actor: str,
        reason: str,
        coordinator: LifecycleTransitionCoordinator,
    ) -> Result[TransitionResult | TransitionPending, TransitionError]:
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            return Err(
                error=TransitionError(
                    code="not_found",
                    message=f"artifact {object_id!r} not found in namespace {namespace!r}",
                    to_state=to_state,
                )
            )
        return transition(
            self._client,
            coordinator=coordinator,
            object_id=object_id,
            target_state=to_state,
            actor=actor,
            reason=reason,
            expected_version=current.version,
        )


__all__ = ["ArtifactPlane"]
