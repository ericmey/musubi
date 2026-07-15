"""C4 / ART-001 production contract: upload -> durable indexing intent -> chunk -> embed ->
stage -> publish -> retrieve, with a single committed generation per artifact.

This is the *smallest production* contract (Yua binding scope): drive the real ArtifactIndexer
through the real lifecycle coordinator's ``reconcile_once`` worker (additive intent-kind, not a new
engine), and assert an artifact goes ``indexing -> indexed`` with exactly one committed generation,
head-first generation+owner-filtered reads, and a working semantic retrieve. It fails RED today
because the production upload path never indexes and reads are unfenced.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from qdrant_client import QdrantClient, models

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import LifecycleTransitionCoordinator
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.artifact.indexer import ArtifactIndexer
from musubi.planes.artifact.plane import _point_id, _sparse_to_model
from musubi.store import bootstrap
from musubi.store.specs import DENSE_VECTOR_NAME, SPARSE_VECTOR_NAME
from musubi.types.artifact import ArtifactChunk, SourceArtifact
from musubi.types.common import generate_ksuid, utc_now

_CONTENT = (
    "# Alpha\nThe alpha section covers onboarding.\n\n# Beta\nThe beta section covers billing.\n"
)


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def plane(qdrant: QdrantClient) -> ArtifactPlane:
    return ArtifactPlane(client=qdrant, embedder=FakeEmbedder())


def _artifact(namespace: str = "eric/dev/artifact") -> SourceArtifact:
    now = utc_now()
    return SourceArtifact(
        object_id=generate_ksuid(),
        namespace=namespace,
        created_at=now,
        updated_at=now,
        title="onboarding",
        filename="onboarding.md",
        sha256="a" * 64,
        content_type="text/markdown",
        size_bytes=len(_CONTENT.encode()),
        chunker="markdown-headings-v1",
    )


def _write_blob(blob_root: Path, art: SourceArtifact, content: str) -> None:
    p = blob_root / art.namespace / art.object_id
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.encode())


@pytest.mark.asyncio
async def test_c4_upload_to_index_to_retrieve_single_committed_generation(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    indexer = ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path)
    indexer.register(coord)  # additive: coord dispatches 'artifact_index' intents to this handler

    # upload: create the head (indexing) + persist the canonical blob + enqueue a durable intent.
    art = await plane.create(_artifact())
    assert art.artifact_state == "indexing"
    _write_blob(tmp_path, art, _CONTENT)
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)

    # run the real worker (one reconcile pass claims + drives the intent to a terminal outcome).
    coord.reconcile_once()

    # head observed indexing -> indexed, naming exactly one committed generation + owner.
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None
    assert head.artifact_state == "indexed", head.artifact_state
    assert head.committed_generation and head.committed_owner
    assert head.chunk_count >= 1

    # list chunks: head-first, filtered to the committed (generation, owner) — one coherent set.
    chunks = await plane.chunks_for(namespace=art.namespace, object_id=art.object_id)
    assert len(chunks) == head.chunk_count
    assert {c.generation for c in chunks} == {head.committed_generation}
    assert {c.owner_token for c in chunks} == {head.committed_owner}

    # retrieve one semantically — a committed chunk of this artifact comes back.
    hits = await plane.query(namespace=art.namespace, query="onboarding", limit=5)
    assert any(h.artifact_id == art.object_id for h in hits)


@pytest.mark.asyncio
async def test_reindex_from_more_to_fewer_hides_old_tail(plane: ArtifactPlane) -> None:
    """Invariant #2 (the ART-001 bug): re-indexing from more chunks to fewer switches the visible
    generation atomically and hides the old tail; head chunk_count == visible committed count (#8)."""
    art = await plane.create(_artifact().model_copy(update={"chunker": "token-sliding-v1"}))
    first = await plane.index(
        art, "alpha beta gamma delta " * 300
    )  # long -> multiple sliding chunks
    assert first.chunk_count >= 2
    second = await plane.index(first, "just a short tail now")
    assert second.committed_generation and second.committed_generation != first.committed_generation
    chunks = await plane.chunks_for(namespace=art.namespace, object_id=art.object_id)
    assert len(chunks) == second.chunk_count  # #8: head count == visible count
    assert {c.generation for c in chunks} == {second.committed_generation}  # no old tail
    assert all(c.owner_token == second.committed_owner for c in chunks)


@pytest.mark.asyncio
async def test_first_ever_index_failure_exposes_zero_chunks(plane: ArtifactPlane) -> None:
    """Invariant #4: a first-ever failed index (un-indexable content) fails closed — the head has no
    committed generation and ZERO chunks are exposed."""
    art = await plane.create(_artifact())
    failed = await plane.index(art, "")  # empty -> chunking yields nothing -> failure
    assert failed.artifact_state == "failed"
    assert failed.committed_generation is None
    assert await plane.chunks_for(namespace=art.namespace, object_id=art.object_id) == []
    assert await plane.query(namespace=art.namespace, query="alpha", limit=5) == []


@pytest.mark.asyncio
async def test_reindex_failure_keeps_previous_generation_visible(plane: ArtifactPlane) -> None:
    """Invariant #3: a FAILED re-index of an already-committed head leaves the PREVIOUS-GOOD generation
    visible and the failed attempt invisible."""
    art = await plane.create(_artifact())
    good = await plane.index(art, "# A\nalpha content here\n")
    g1 = good.committed_generation
    assert g1
    after = await plane.index(good, "")  # un-indexable re-index -> fails
    assert after.committed_generation == g1  # prior good retained
    chunks = await plane.chunks_for(namespace=art.namespace, object_id=art.object_id)
    assert len(chunks) == good.chunk_count and {c.generation for c in chunks} == {g1}


@pytest.mark.asyncio
async def test_legacy_indexed_head_deserializes_and_fails_closed_then_reindexes(
    qdrant: QdrantClient, plane: ArtifactPlane
) -> None:
    """Yua rollout-safety: a LEGACY indexed head (no committed generation) with generation-less chunks
    (a) still model-loads, (b) never exposes its generation-less chunks (fail-closed), and (c) a
    reindex produces a committed head whose chunks ARE exposed."""
    art = _artifact()
    legacy_head = art.model_copy(update={"artifact_state": "indexed", "chunk_count": 1})
    # (a) backward deserialization: a legacy indexed head with committed_generation=None loads + persists.
    await plane.create(legacy_head)
    loaded = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert loaded is not None
    assert loaded.artifact_state == "indexed" and loaded.committed_generation is None

    # a generation-less legacy chunk, embedded so it is a genuine search candidate.
    fake = FakeEmbedder()
    dense = (await fake.embed_dense(["alpha legacy chunk"]))[0]
    sparse = (await fake.embed_sparse(["alpha legacy chunk"]))[0]
    legacy_chunk = ArtifactChunk(
        chunk_id=generate_ksuid(),
        artifact_id=art.object_id,
        chunk_index=0,
        content="alpha legacy chunk",
        start_offset=0,
        end_offset=18,
    )
    payload = legacy_chunk.model_dump(mode="json")
    payload["namespace"] = art.namespace
    qdrant.upsert(
        collection_name="musubi_artifact_chunks",
        points=[
            models.PointStruct(
                id=_point_id(legacy_chunk.chunk_id),
                payload=payload,
                vector={
                    DENSE_VECTOR_NAME: dense,
                    SPARSE_VECTOR_NAME: _sparse_to_model(sparse),
                },
            )
        ],
    )

    # (b) fail-closed: the generation-less legacy chunk is NEVER exposed.
    assert await plane.chunks_for(namespace=art.namespace, object_id=art.object_id) == []
    assert await plane.query(namespace=art.namespace, query="alpha legacy chunk", limit=5) == []

    # (c) reindex -> committed head, exposed.
    reindexed = await plane.index(loaded, "# A\nalpha migrated content\n")
    assert reindexed.committed_generation and reindexed.artifact_state == "indexed"
    committed = await plane.chunks_for(namespace=art.namespace, object_id=art.object_id)
    assert len(committed) == reindexed.chunk_count
    assert {c.generation for c in committed} == {reindexed.committed_generation}


async def _stage_noncommitted(
    qdrant: QdrantClient, namespace: str, artifact_id: str, count: int, text: str
) -> None:
    """Insert ``count`` search-candidate chunks tagged with a non-current generation (no committed
    head names them) — the raw material for a generation_churn / fail-closed scenario."""
    fake = FakeEmbedder()
    dense = (await fake.embed_dense([text]))[0]
    sparse = (await fake.embed_sparse([text]))[0]
    pts = []
    for i in range(count):
        cid = generate_ksuid()
        ch = ArtifactChunk(
            chunk_id=cid,
            artifact_id=artifact_id,
            chunk_index=i,
            content=f"{text} {i}",
            start_offset=0,
            end_offset=5,
            generation="stale-generation",
            owner_token="stale-owner",
        )
        payload = ch.model_dump(mode="json")
        payload["namespace"] = namespace
        pts.append(
            models.PointStruct(
                id=_point_id(cid),
                payload=payload,
                vector={DENSE_VECTOR_NAME: dense, SPARSE_VECTOR_NAME: _sparse_to_model(sparse)},
            )
        )
    qdrant.upsert(collection_name="musubi_artifact_chunks", points=pts)


@pytest.mark.asyncio
async def test_query_with_degradation_warns_generation_churn_when_budget_saturated(
    qdrant: QdrantClient, plane: ArtifactPlane
) -> None:
    """Accepted global-search contract: when the candidate budget is saturated with NON-current
    chunks (even after the one bounded retry) and ``limit`` is under-filled, return the bounded partial
    plus an explicit ``generation_churn`` warning — never silent false completeness. All returned
    chunks are committed."""
    ns = "eric/dev/artifact"
    await _stage_noncommitted(qdrant, ns, generate_ksuid(), 120, "churn target")
    results, warns = await plane.query_with_degradation(
        namespace=ns, query="churn target", limit=10
    )
    assert "generation_churn" in warns
    assert results == []  # nothing is committed — fail-closed


@pytest.mark.asyncio
async def test_query_with_degradation_no_warning_when_genuinely_sparse(
    plane: ArtifactPlane,
) -> None:
    """A genuinely sparse result (fewer than ``limit`` committed chunks exist, budget NOT exhausted) is
    complete, not churn — no warning."""
    art = await plane.create(_artifact())
    await plane.index(art, "# A\none small committed chunk\n")
    results, warns = await plane.query_with_degradation(
        namespace=art.namespace, query="committed chunk", limit=10
    )
    assert warns == []
    assert len(results) >= 1 and all(r.artifact_id == art.object_id for r in results)


@pytest.mark.asyncio
async def test_async_index_empty_content_fails_closed(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """The async worker on un-indexable (empty) content drives the intent to a terminal FAILED head,
    fail-closed: no committed generation, zero chunks exposed."""
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path).register(coord)
    art = await plane.create(_artifact())
    _write_blob(tmp_path, art, "")  # empty -> chunking yields nothing
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    coord.reconcile_once()
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.artifact_state == "failed"
    assert head.committed_generation is None
    assert await plane.chunks_for(namespace=art.namespace, object_id=art.object_id) == []


@pytest.mark.asyncio
async def test_publish_failed_fences_on_stale_publication_version(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """_publish_failed reads back and returns a NON-confirmed outcome when its fenced write matches
    zero (a concurrent winner already advanced publication_version) — a loser is never finalized."""
    from musubi.lifecycle.coordinator import CustomIntentContext

    art = await plane.create(_artifact())
    # simulate a concurrent winner: the stored head is at publication_version=5.
    qdrant.set_payload(
        collection_name="musubi_artifact",
        payload={"publication_version": 5},
        points=[_point_id(art.object_id)],
    )
    indexer = ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path)
    ctx = CustomIntentContext(
        operation_key="opk-loser",
        object_id=art.object_id,
        collection="musubi_artifact",
        namespace=art.namespace,
        owner_token="owner-loser",
    )
    stale_head = art.model_copy(update={"publication_version": 0})  # stale view
    outcome = await indexer._publish_failed(stale_head, ctx, "empty")
    assert outcome in ("fence", "retry")  # matched-zero fence — NOT confirmed
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.publication_version == 5  # winner's head untouched


@pytest.mark.asyncio
async def test_async_index_invalid_utf8_fails_closed(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """Deterministic content failure (invalid UTF-8) is a TERMINAL failed head, not a coordinator
    ABANDON that leaves the head stuck indexing. Fail-closed: no committed generation."""
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path).register(coord)
    art = await plane.create(_artifact())
    (tmp_path / art.namespace).mkdir(parents=True, exist_ok=True)
    (tmp_path / art.namespace / art.object_id).write_bytes(b"\xff\xfe not valid utf-8 \xff")
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    coord.reconcile_once()
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.artifact_state == "failed"
    assert head.committed_generation is None
    assert await plane.chunks_for(namespace=art.namespace, object_id=art.object_id) == []


@pytest.mark.asyncio
async def test_async_index_unknown_chunker_fails_closed(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """Deterministic config failure (unknown chunker) is a TERMINAL failed head — never silently
    mis-chunked under the default."""
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path).register(coord)
    art = await plane.create(_artifact().model_copy(update={"chunker": "bogus-chunker-v9"}))
    _write_blob(tmp_path, art, "# A\nsome content\n")
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    coord.reconcile_once()
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.artifact_state == "failed"
    assert head.committed_generation is None


@pytest.mark.asyncio
async def test_async_index_transient_embed_failure_retries_not_failed(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """A TRANSIENT embed/Qdrant failure must NOT mark the head failed — it propagates so the coordinator
    reschedules (retry); the head stays indexing."""

    class BoomEmbedder(FakeEmbedder):
        async def embed_dense(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("TEI unavailable")

    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    ArtifactIndexer(client=qdrant, embedder=BoomEmbedder(), blob_root=tmp_path).register(coord)
    art = await plane.create(_artifact())
    _write_blob(tmp_path, art, "# A\ncontent to embed\n")
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    coord.reconcile_once()
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.artifact_state == "indexing"  # NOT failed — transient → retry
    assert head.committed_generation is None


@pytest.mark.asyncio
async def test_async_reindex_reclaims_only_prior_generation(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """A confirmed async re-index reclaims ONLY the exact superseded prior (generation, owner) — a
    different artifact's chunks are never touched."""
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path).register(coord)

    art_a = await plane.create(_artifact())
    _write_blob(tmp_path, art_a, "# A\nalpha original content\n")
    coord.enqueue_index_intent(object_id=art_a.object_id, namespace=art_a.namespace)
    coord.reconcile_once()
    head_a1 = await plane.get(namespace=art_a.namespace, object_id=art_a.object_id)
    assert head_a1 is not None
    g1 = head_a1.committed_generation
    assert g1 is not None

    art_b = await plane.create(_artifact())
    _write_blob(tmp_path, art_b, "# B\nbeta content stays\n")
    coord.enqueue_index_intent(object_id=art_b.object_id, namespace=art_b.namespace)
    coord.reconcile_once()
    b_before = await plane.chunks_for(namespace=art_b.namespace, object_id=art_b.object_id)

    _write_blob(tmp_path, art_a, "# A\nalpha REPLACED content v2\n")
    coord.enqueue_index_intent(object_id=art_a.object_id, namespace=art_a.namespace)
    coord.reconcile_once()
    head_a2 = await plane.get(namespace=art_a.namespace, object_id=art_a.object_id)
    assert (
        head_a2 is not None and head_a2.committed_generation and head_a2.committed_generation != g1
    )

    # prior generation g1 reclaimed (scoped delete)
    g1_left, _ = qdrant.scroll(
        collection_name="musubi_artifact_chunks",
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="artifact_id", match=models.MatchValue(value=art_a.object_id)
                ),
                models.FieldCondition(key="generation", match=models.MatchValue(value=g1)),
            ]
        ),
        limit=100,
    )
    assert g1_left == []
    # different artifact B untouched
    b_after = await plane.chunks_for(namespace=art_b.namespace, object_id=art_b.object_id)
    assert len(b_after) == len(b_before) and len(b_after) >= 1


@pytest.mark.asyncio
async def test_enqueue_at_capacity_marks_head_failed_visible_terminal(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """When the outbox is at capacity, enqueue returns 'at_capacity' and the head is recorded as a
    VISIBLE terminal failed — never silently stuck indexing."""
    coord = LifecycleTransitionCoordinator(
        client=qdrant, db_path=tmp_path / "coord.db", pending_cap=1
    )
    art = await plane.create(_artifact())
    # fill the single-slot cap with an index intent for a different artifact
    assert (
        coord.enqueue_index_intent(object_id=generate_ksuid(), namespace=art.namespace)
        == "admitted"
    )
    status = coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    assert status == "at_capacity"
    failed = await plane.mark_index_unadmitted(art)
    assert failed.artifact_state == "failed" and failed.committed_generation is None
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.artifact_state == "failed" and head.failure_reason


@pytest.mark.asyncio
async def test_sync_index_unknown_chunker_fails_closed(plane: ArtifactPlane) -> None:
    """Copilot #1: the SYNC index() rejects an unknown chunker deterministically (no silent default
    fallback) → terminal failed head, no committed generation, ZERO visible chunks."""
    art = await plane.create(_artifact().model_copy(update={"chunker": "bogus-sync-chunker-v9"}))
    result = await plane.index(art, "# A\nsome content to chunk\n")
    assert result.artifact_state == "failed"
    assert result.committed_generation is None
    assert "unknown chunker" in (result.failure_reason or "")
    assert await plane.chunks_for(namespace=art.namespace, object_id=art.object_id) == []


@pytest.mark.asyncio
async def test_sync_index_clears_stale_index_operation_id(
    qdrant: QdrantClient, plane: ArtifactPlane
) -> None:
    """Copilot #2: EVERY sync head write clears index_operation_id (the sync path has no async intent),
    so a stale prior op id never lingers — on success, re-index failure (prior preserved), and
    first-failure fail-closed."""
    art = await plane.create(_artifact())
    good = await plane.index(art, "# A\nalpha content\n")
    assert good.artifact_state == "indexed" and good.index_operation_id is None  # success clears
    g1 = good.committed_generation

    # stamp a STALE async op id onto the committed head, then re-index (success) → cleared
    qdrant.set_payload(
        collection_name="musubi_artifact",
        payload={"index_operation_id": "stale-opk-success"},
        points=[_point_id(art.object_id)],
    )
    head_stale = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head_stale is not None and head_stale.index_operation_id == "stale-opk-success"
    good2 = await plane.index(head_stale, "# A\nalpha v2 content\n")
    assert good2.committed_generation != g1 and good2.index_operation_id is None
    g2 = good2.committed_generation

    # re-index FAILURE (empty) on a head with a stale op id → prior preserved + op id cleared
    qdrant.set_payload(
        collection_name="musubi_artifact",
        payload={"index_operation_id": "stale-opk-fail"},
        points=[_point_id(art.object_id)],
    )
    head_stale2 = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head_stale2 is not None
    after_fail = await plane.index(
        head_stale2, ""
    )  # empty → re-index fails, prior preserved (inv #3)
    assert after_fail.committed_generation == g2 and after_fail.index_operation_id is None

    # first-ever failure on a fresh head carrying a stale op id → failed + op id None
    fresh = await plane.create(_artifact())
    qdrant.set_payload(
        collection_name="musubi_artifact",
        payload={"index_operation_id": "stale-opk-first"},
        points=[_point_id(fresh.object_id)],
    )
    fresh_stale = await plane.get(namespace=fresh.namespace, object_id=fresh.object_id)
    assert fresh_stale is not None
    failed = await plane.index(fresh_stale, "")  # first-index fail (empty)
    assert failed.artifact_state == "failed"
    assert failed.committed_generation is None and failed.index_operation_id is None


@pytest.mark.asyncio
async def test_async_replay_after_first_index_failure_is_idempotent_no_pv_bump(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """Copilot #1: replay of a PUBLISHED first-index deterministic failure recognizes the same
    operation_key as terminal → confirms WITHOUT re-publishing or bumping publication_version."""
    from musubi.lifecycle.coordinator import CustomIntentContext

    indexer = ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path)
    art = await plane.create(_artifact())
    (tmp_path / art.namespace).mkdir(parents=True, exist_ok=True)
    (tmp_path / art.namespace / art.object_id).write_bytes(b"")  # empty -> deterministic failure
    ctx = CustomIntentContext(
        operation_key="op-replay-first",
        object_id=art.object_id,
        collection="musubi_artifact",
        namespace=art.namespace,
        owner_token="owner-a",
    )
    assert await indexer._apply_async(ctx) == "confirmed"
    h1 = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert h1 is not None and h1.artifact_state == "failed"
    assert h1.index_operation_id == "op-replay-first"
    pv1 = h1.publication_version

    assert await indexer._apply_async(ctx) == "confirmed"  # REPLAY same op
    h2 = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert h2 is not None and h2.publication_version == pv1  # NOT bumped
    assert h2.artifact_state == "failed"


@pytest.mark.asyncio
async def test_async_replay_after_reindex_keeps_prior_failure_is_idempotent(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """Copilot #1: replay of a PUBLISHED re-index failure (prior generation kept visible) is idempotent
    — confirms, no pv bump, prior generation still visible."""
    from musubi.lifecycle.coordinator import CustomIntentContext

    indexer = ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path)
    art = await plane.create(_artifact())
    _write_blob(tmp_path, art, "# A\ngood content\n")
    ctx1 = CustomIntentContext(
        operation_key="op1",
        object_id=art.object_id,
        collection="musubi_artifact",
        namespace=art.namespace,
        owner_token="owner1",
    )
    assert await indexer._apply_async(ctx1) == "confirmed"
    _h = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert _h is not None
    g1 = _h.committed_generation

    (tmp_path / art.namespace / art.object_id).write_bytes(
        b""
    )  # empty -> re-index deterministic fail
    ctx2 = CustomIntentContext(
        operation_key="op2",
        object_id=art.object_id,
        collection="musubi_artifact",
        namespace=art.namespace,
        owner_token="owner2",
    )
    assert await indexer._apply_async(ctx2) == "confirmed"
    h2 = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert h2 is not None and h2.committed_generation == g1 and h2.index_operation_id == "op2"
    pv2 = h2.publication_version

    assert await indexer._apply_async(ctx2) == "confirmed"  # REPLAY op2
    h3 = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert h3 is not None and h3.publication_version == pv2 and h3.committed_generation == g1


@pytest.mark.asyncio
async def test_async_success_clears_stale_failure_reason(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """Copilot #2: a successful async publish clears a stale prior failure_reason."""
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    ArtifactIndexer(client=qdrant, embedder=FakeEmbedder(), blob_root=tmp_path).register(coord)
    art = await plane.create(_artifact())
    qdrant.set_payload(
        collection_name="musubi_artifact",
        payload={"failure_reason": "an old failure that must not survive"},
        points=[_point_id(art.object_id)],
    )
    _write_blob(tmp_path, art, "# A\ngood content now\n")
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    coord.reconcile_once()
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.artifact_state == "indexed" and head.failure_reason is None


@pytest.mark.asyncio
async def test_sync_index_partial_upsert_raises_gc_staged_generation(
    qdrant: QdrantClient, plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copilot #4: staged_generation is set BEFORE the chunk upsert, so a timeout/partial server-side
    success still GCs the exact staged generation (no orphan tail)."""
    art = await plane.create(_artifact())
    real_upsert_any = cast(Any, qdrant.upsert)

    def partial_then_raise(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("collection_name") == "musubi_artifact_chunks":
            real_upsert_any(*args, **kwargs)  # partial server-side write
            raise RuntimeError("timeout AFTER partial upsert")
        return real_upsert_any(*args, **kwargs)

    monkeypatch.setattr(qdrant, "upsert", partial_then_raise)
    result = await plane.index(art, "# A\ncontent to stage\n")
    monkeypatch.undo()
    assert result.artifact_state == "failed"  # first-index failure, fail-closed
    left, _ = qdrant.scroll(
        collection_name="musubi_artifact_chunks",
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="artifact_id", match=models.MatchValue(value=art.object_id)
                )
            ]
        ),
        limit=100,
    )
    assert left == []  # the partially-upserted staged generation was cleaned (blocker 4)


def test_ensure_schema_tolerates_two_process_duplicate_intent_kind_race(tmp_path: Path) -> None:
    """Copilot #5: a two-process duplicate-column race on the intent_kind ALTER is tolerated, but any
    OTHER OperationalError still surfaces (never masked). (sqlite3.Connection.execute is read-only, so a
    thin proxy models process A: PRAGMA reports the column ABSENT while the ALTER hits it already
    present.)"""
    import sqlite3

    from musubi.lifecycle import store

    class _FakeCursor:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self._rows

    class _ProxyConn:
        def __init__(self, real: sqlite3.Connection, alter_error: Exception) -> None:
            self._real = real
            self._alter_error = alter_error

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            sql = str(args[0]) if args else str(kwargs.get("sql", ""))
            if "PRAGMA table_info(lifecycle_outbox)" in sql:
                return _FakeCursor([(0, "operation_key", "TEXT", 0, None, 1)])  # no intent_kind row
            if sql.strip().upper().startswith("ALTER TABLE"):
                raise self._alter_error
            return self._real.execute(*args, **kwargs)

        def executescript(self, sql: str) -> Any:
            return self._real.executescript(sql)

        def commit(self) -> None:
            self._real.commit()

    real = store.connect(tmp_path / "race.db")
    store.ensure_schema(real)  # intent_kind now exists

    # RACE: PRAGMA reports absent → ensure_schema ALTERs an existing column → duplicate → TOLERATED.
    dup = sqlite3.OperationalError("duplicate column name: intent_kind")
    store.ensure_schema(_ProxyConn(real, dup))  # type: ignore[arg-type]  # must NOT raise

    # DISCRIMINATION: a NON-duplicate OperationalError must propagate, never be swallowed.
    locked = sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        store.ensure_schema(_ProxyConn(real, locked))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unknown_handler_outcome_abandons_not_infinite_retry(
    qdrant: QdrantClient, plane: ArtifactPlane, tmp_path: Path
) -> None:
    """Copilot #6: a handler returning an outcome outside {confirmed,fence,retry} fails FAST (ABANDONED),
    never an infinite retry."""
    coord = LifecycleTransitionCoordinator(client=qdrant, db_path=tmp_path / "coord.db")
    coord.register_intent_handler("artifact_index", lambda ctx: "not-a-valid-outcome")
    art = await plane.create(_artifact())
    coord.enqueue_index_intent(object_id=art.object_id, namespace=art.namespace)
    report = coord.reconcile_once()
    assert report.abandoned == 1 and report.pending == 0  # terminal, not retried
    report2 = coord.reconcile_once()
    assert report2.claimed == 0  # ABANDONED intent is terminal — never re-claimed


def _winner_injecting_set_payload(qdrant: QdrantClient, object_id: str) -> object:
    """Wrap the test client's set_payload so the FIRST filtered head publish is preceded by a concurrent
    winner advancing publication_version + committing a different generation — forcing a fence loss in
    the reread->write window. Returns the wrapper; caller installs it via monkeypatch."""
    real = qdrant.set_payload
    state = {"injected": False}

    def racing(*args: Any, **kwargs: Any) -> Any:
        if (
            kwargs.get("collection_name") == "musubi_artifact"
            and isinstance(kwargs.get("points"), models.Filter)
            and not state["injected"]
        ):
            state["injected"] = True
            real(
                collection_name="musubi_artifact",
                payload={
                    "publication_version": 99,
                    "committed_generation": "winner-gen",
                    "committed_owner": "winner-owner",
                    "artifact_state": "indexed",
                    "chunk_count": 1,
                },
                points=[_point_id(object_id)],
            )
        return real(*args, **kwargs)

    return racing


@pytest.mark.asyncio
async def test_sync_index_success_fence_loss_preserves_winner(
    qdrant: QdrantClient, plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copilot #3: a stale-caller SUCCESS publish loses the publication_version fence to a concurrent
    winner — it deletes only its own generation, preserves the winner, and returns the winner's head."""
    art = await plane.create(_artifact())
    base = await plane.index(art, "# A\nbase content\n")  # pv=1, G1
    monkeypatch.setattr(qdrant, "set_payload", _winner_injecting_set_payload(qdrant, art.object_id))
    result = await plane.index(base, "# A\nour losing content\n")
    monkeypatch.undo()
    assert result.committed_generation == "winner-gen"  # returned the winner, not our attempt
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.committed_generation == "winner-gen"
    assert head.publication_version == 99  # winner's pv preserved, never regressed
    chunks = await plane.chunks_for(namespace=art.namespace, object_id=art.object_id)
    assert all(c.generation == "winner-gen" for c in chunks)  # our losing generation was cleaned


@pytest.mark.asyncio
async def test_sync_index_failure_fence_loss_preserves_winner(
    qdrant: QdrantClient, plane: ArtifactPlane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copilot #3 (failure path): a stale-caller FAILURE write also loses the fence to a concurrent
    winner — it never clobbers the winner or regresses publication_version."""
    art = await plane.create(_artifact())
    base = await plane.index(art, "# A\nbase content\n")  # pv=1, G1
    monkeypatch.setattr(qdrant, "set_payload", _winner_injecting_set_payload(qdrant, art.object_id))
    result = await plane.index(
        base, ""
    )  # empty -> FAILS -> fenced failure write loses to the winner
    monkeypatch.undo()
    assert result.committed_generation == "winner-gen"
    head = await plane.get(namespace=art.namespace, object_id=art.object_id)
    assert head is not None and head.committed_generation == "winner-gen"
    assert head.publication_version == 99 and head.artifact_state == "indexed"  # winner intact
