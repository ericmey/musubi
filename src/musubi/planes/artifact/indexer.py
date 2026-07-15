"""C4 / ART-001 committed-generation artifact indexer.

Replaces the legacy unfenced ``ArtifactPlane.index()`` publish. Registered with the lifecycle
coordinator as the ``'artifact_index'`` intent handler: the coordinator owns the durable intent
lifecycle (admission, claim/lease, attempts/backoff, reconcile, terminal) and hands each claimed
intent to :meth:`ArtifactIndexer.apply`, which:

1. reads the artifact head and short-circuits if THIS intent already committed (idempotent re-drive);
2. loads the canonical blob, chunks + embeds it;
3. mints a NEVER-REUSED ``generation`` for this attempt (owner = the coordinator's never-reused lease
   token) and stages all chunks tagged ``(generation, owner_token)`` — INVISIBLE until published,
   because every read resolves the head first and filters by the committed ``(generation, owner)``;
4. publishes by a ``publication_version``-fenced conditional head replace, then reads the head back —
   exact ``(committed_generation, committed_owner)`` equality is the ONLY success signal (Qdrant's
   conditional response is untrustworthy, per PR #453);
5. a loser/fenced attempt removes ONLY its own ``(generation, owner)`` staged chunks (ABA-safe,
   because the tokens are never reused).

Returns the coordinator's outcome vocabulary: ``'confirmed'`` (won / already-done), ``'fence'``
(lost or a vanished head — terminal), ``'retry'`` (transient — keep PENDING + backoff).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qdrant_client import models

from musubi.embedding.base import Embedder
from musubi.planes.artifact.chunking import KNOWN_CHUNKERS, get_chunker
from musubi.planes.artifact.plane import _artifact_from_payload, _point_id, _sparse_to_model
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.artifact import ArtifactChunk, SourceArtifact
from musubi.types.common import epoch_of, generate_ksuid, utc_now

if TYPE_CHECKING:
    from musubi.lifecycle.coordinator import CustomIntentContext, LifecycleTransitionCoordinator

_ARTIFACT_INDEX_KIND = "artifact_index"


def _run_coro(coro: Any) -> Any:
    """Run an async coroutine to completion from a SYNC caller (the reconcile worker). If a loop is
    already running in this thread (e.g. an async test driving ``reconcile_once``), run it in a fresh
    loop on a worker thread so we never nest event loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


class ArtifactIndexer:
    def __init__(
        self,
        *,
        client: Any,
        embedder: Embedder,
        blob_root: Path | str,
        collection: str = "musubi_artifact",
        chunks_collection: str = "musubi_artifact_chunks",
    ) -> None:
        self._client = client
        self._embedder = embedder
        self._blob_root = Path(blob_root)
        self._collection = collection
        self._chunks_collection = chunks_collection

    def register(self, coordinator: LifecycleTransitionCoordinator) -> None:
        """Wire this indexer as the coordinator's ``'artifact_index'`` apply handler."""
        coordinator.register_intent_handler(_ARTIFACT_INDEX_KIND, self.apply)

    # -- head read / write ----------------------------------------------------------------------- #

    def _read_head(self, object_id: str, namespace: str) -> SourceArtifact | None:
        records, _ = self._client.scroll(
            collection_name=self._collection,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                ]
            ),
            limit=1,
            with_payload=True,
        )
        if not records or not records[0].payload:
            return None
        return _artifact_from_payload(records[0].payload)

    def _delete_generation_chunks(self, object_id: str, generation: str, owner: str) -> None:
        """Remove ONLY the chunks bearing this exact (generation, owner) — ABA-safe loser/GC cleanup."""
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
                    models.FieldCondition(key="owner_token", match=models.MatchValue(value=owner)),
                ]
            ),
        )

    # -- the registered apply handler ------------------------------------------------------------ #

    def apply(self, ctx: CustomIntentContext) -> str:
        outcome = _run_coro(self._apply_async(ctx))
        return str(outcome)

    async def _apply_async(self, ctx: CustomIntentContext) -> str:
        object_id, namespace, owner = ctx.object_id, ctx.namespace, ctx.owner_token
        head = self._read_head(object_id, namespace)
        if head is None:
            return "fence"  # the head vanished — nothing to index; terminal.

        # Capture the EXACT superseded pair from the old head, so a confirmed publish reclaims only it.
        prior_generation, prior_owner = head.committed_generation, head.committed_owner

        # Idempotent re-drive: a prior attempt of THIS intent already published a committed head.
        if head.artifact_state == "indexed" and head.index_operation_id == ctx.operation_key:
            return "confirmed"

        blob = self._blob_root / namespace / object_id
        if not blob.exists():
            return "retry"  # canonical blob not durable yet — transient; reconcile will retry.

        # Deterministic content/config failures are TERMINAL (a publication_version-fenced FAILED head),
        # never a coordinator ABANDON that would leave the head stuck 'indexing'. Transient embed/Qdrant
        # failures below are NOT caught here — they propagate so the coordinator reschedules (retry).
        try:
            if head.chunker not in KNOWN_CHUNKERS:
                raise ValueError(f"unknown chunker: {head.chunker!r}")
            content = blob.read_bytes().decode("utf-8")
            raw = get_chunker(head.chunker).chunk(content)
        except (UnicodeDecodeError, ValueError, LookupError, KeyError) as exc:
            return await self._publish_failed(head, ctx, f"content/config error: {exc}")
        if not raw:
            return await self._publish_failed(head, ctx, "chunking produced no chunks")

        generation = secrets.token_hex(16)  # NEVER reused — the ABA fence (with owner).
        texts = [rc.content for rc in raw]
        dense = await self._embedder.embed_dense(texts)
        sparse = await self._embedder.embed_sparse(texts)

        # Stage every chunk INVISIBLY under (generation, owner). Reads filter by the committed head,
        # so nothing here is exposed until — and unless — the fenced publish below wins.
        points: list[models.PointStruct] = []
        for i, rc in enumerate(raw):
            chunk_id = generate_ksuid()
            chunk = ArtifactChunk(
                chunk_id=chunk_id,
                artifact_id=object_id,
                chunk_index=rc.index,
                content=rc.content,
                start_offset=rc.start_offset,
                end_offset=rc.end_offset,
                chunk_metadata=rc.metadata,
                generation=generation,
                owner_token=owner,
            )
            payload = chunk.model_dump(mode="json")
            payload["namespace"] = namespace
            payload["content_type"] = head.content_type
            payload["chunker"] = head.chunker
            points.append(
                models.PointStruct(
                    id=_point_id(chunk_id),
                    payload=payload,
                    vector={
                        DENSE_VECTOR_NAME: dense[i],
                        SPARSE_VECTOR_NAME: _sparse_to_model(sparse[i]),
                    },
                )
            )
        self._client.upsert(collection_name=self._chunks_collection, points=points)

        # Conditional head publish, fenced on the previously-observed publication_version. A stale/lost
        # publisher matches zero points; the readback (not this call's response) is the arbiter.
        expected_pv = head.publication_version
        now = utc_now()
        self._client.set_payload(
            collection_name=self._collection,
            payload={
                "artifact_state": "indexed",
                "chunk_count": len(raw),
                "committed_generation": generation,
                "committed_owner": owner,
                "index_operation_id": ctx.operation_key,
                "publication_version": expected_pv + 1,
                "updated_at": now.isoformat(),
                "updated_epoch": epoch_of(now),
            },
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=namespace)
                    ),
                    models.FieldCondition(
                        key="publication_version", match=models.MatchValue(value=expected_pv)
                    ),
                ]
            ),
        )

        # Exact head readback is the ONLY success signal (PR #453).
        published = self._read_head(object_id, namespace)
        if (
            published is not None
            and published.committed_generation == generation
            and published.committed_owner == owner
        ):
            # Reclaim ONLY the EXACT superseded prior (generation, owner) captured from the old head —
            # scoped, never a broad delete that could hit a concurrent attempt's fresh generation.
            if (
                prior_generation
                and prior_owner
                and (prior_generation, prior_owner) != (generation, owner)
            ):
                self._delete_generation_chunks(object_id, prior_generation, prior_owner)
            return "confirmed"
        # Lost / fenced: remove ONLY this attempt's staged chunks (never touch the winner's).
        self._delete_generation_chunks(object_id, generation, owner)
        return "fence"

    async def _publish_failed(
        self, head: SourceArtifact, ctx: CustomIntentContext, reason: str
    ) -> str:
        """Terminal indexing FAILURE (e.g. empty chunking), publication_version-fenced. First-ever
        failure fails CLOSED (no committed generation). A re-index failure keeps the PREVIOUS-GOOD
        committed generation VISIBLE (inv #3) — the committed head is not cleared, only the failed
        attempt is recorded. Returns ``'confirmed'`` ONLY after reading the head back and proving THIS
        attempt's terminal write landed at its fence; a matched-zero (lost) fence returns
        ``'fence'``/``'retry'`` so a loser is never finalized."""
        expected_pv = head.publication_version
        now = utc_now()
        payload: dict[str, Any] = {
            "failure_reason": reason,
            "index_operation_id": ctx.operation_key,
            "publication_version": expected_pv + 1,
            "updated_at": now.isoformat(),
            "updated_epoch": epoch_of(now),
        }
        if not head.committed_generation:
            # first-ever failure: fail-closed. (A re-index keeps its prior committed generation.)
            payload["artifact_state"] = "failed"
        self._client.set_payload(
            collection_name=self._collection,
            payload=payload,
            points=models.Filter(
                must=[
                    models.FieldCondition(
                        key="object_id", match=models.MatchValue(value=ctx.object_id)
                    ),
                    models.FieldCondition(
                        key="namespace", match=models.MatchValue(value=ctx.namespace)
                    ),
                    models.FieldCondition(
                        key="publication_version", match=models.MatchValue(value=expected_pv)
                    ),
                ]
            ),
        )
        # Readback: confirm ONLY if this attempt's own terminal write landed at its fence.
        published = self._read_head(ctx.object_id, ctx.namespace)
        if published is None:
            return "fence"
        if (
            published.index_operation_id == ctx.operation_key
            and published.publication_version == expected_pv + 1
        ):
            return "confirmed"
        if (
            published.artifact_state == "indexed"
            and published.index_operation_id != ctx.operation_key
        ):
            return "fence"  # a concurrent winner published a different generation — this attempt is moot
        return "retry"  # our fenced write matched zero (pv advanced elsewhere) — retry


__all__ = ["ArtifactIndexer"]
