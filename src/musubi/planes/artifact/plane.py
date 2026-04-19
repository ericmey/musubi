"""``ArtifactPlane`` — CRUD + chunking for :class:`SourceArtifact`.

Manages the `musubi_artifact` (metadata) and `musubi_artifact_chunks`
(searchable content) collections.
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import QdrantClient, models

from musubi.embedding.base import Embedder
from musubi.planes.artifact.chunking import get_chunker
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.artifact import ArtifactChunk, SourceArtifact
from musubi.types.common import (
    KSUID,
    LifecycleState,
    Namespace,
    epoch_of,
    generate_ksuid,
    utc_now,
)
from musubi.types.lifecycle_event import LifecycleEvent

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

        zero_dense = (await self._embedder.embed_dense([""]))[0]
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
        """Chunk the content, embed, and store in chunks collection.
        Updates artifact to indexed state.
        """
        chunker = get_chunker(artifact.chunker)
        try:
            raw_chunks = chunker.chunk(content)
            if not raw_chunks:
                raise ValueError("chunking produced no chunks")

            # Batch embed
            texts = [c.content for c in raw_chunks]
            dense_batch = await self._embedder.embed_dense(texts)
            sparse_batch = await self._embedder.embed_sparse(texts)

            points = []
            chunk_models = []
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
                )
                chunk_models.append(chunk)

                payload = chunk.model_dump(mode="json")
                # Important: Include namespace in chunk payload for filtering
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

            # Update artifact state
            now = utc_now()
            data = artifact.model_dump()
            data.update(
                artifact_state="indexed",
                chunk_count=len(raw_chunks),
                updated_at=now,
                updated_epoch=epoch_of(now),
            )
            updated = SourceArtifact.model_validate(data)
            self._client.set_payload(
                collection_name=self._collection,
                payload=updated.model_dump(mode="json"),
                points=[_point_id(artifact.object_id)],
            )
            return updated

        except Exception as e:
            # On failure, mark state failed
            now = utc_now()
            data = artifact.model_dump()
            data.update(
                artifact_state="failed",
                failure_reason=str(e),
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

    async def query(
        self,
        *,
        namespace: Namespace,
        query: str,
        limit: int = 10,
    ) -> list[ArtifactChunk]:
        """Dense retrieval of chunks filtered to namespace."""
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
            limit=limit,
            with_payload=True,
        )
        out: list[ArtifactChunk] = []
        for point in resp.points:
            if point.payload:
                out.append(_chunk_from_payload(point.payload))
        return out

    async def query_by_artifact(self, *, artifact_id: KSUID) -> list[ArtifactChunk]:
        """Fetch all chunks for an artifact_id, ordered by index."""
        records, _ = self._client.scroll(
            collection_name=self._chunks_collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="artifact_id", match=models.MatchValue(value=artifact_id)
                    ),
                ]
            ),
            limit=10000,
            with_payload=True,
        )
        chunks = []
        for r in records:
            if r.payload:
                chunks.append(_chunk_from_payload(r.payload))
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
    ) -> tuple[SourceArtifact, LifecycleEvent]:
        current = await self.get(namespace=namespace, object_id=object_id)
        if current is None:
            raise LookupError(f"artifact {object_id!r} not found in namespace {namespace!r}")

        event = LifecycleEvent(
            object_id=object_id,
            object_type="artifact",
            namespace=namespace,
            from_state=current.state,
            to_state=to_state,
            actor=actor,
            reason=reason,
        )
        now = utc_now()
        data = current.model_dump()
        data.update(
            state=to_state,
            version=current.version + 1,
            updated_at=now,
            updated_epoch=epoch_of(now),
        )
        updated = SourceArtifact.model_validate(data)
        self._client.set_payload(
            collection_name=self._collection,
            payload=updated.model_dump(mode="json"),
            points=[_point_id(object_id)],
        )
        return updated, event


__all__ = ["ArtifactPlane"]
