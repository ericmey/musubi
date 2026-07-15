"""C4 / ART-001 real-Qdrant concurrency proof (invariant #5).

``:memory:`` Qdrant cannot serialize the fenced conditional publish under real concurrency, so the
single-winner property must be proven against a real server (the ART-001 spikes used the same
rationale). Marked ``integration`` — excluded from the default unit run, executed against the
docker-compose Qdrant. Bring it up with ``MUSUBI_TEST_QDRANT_PORT=6339 docker compose -f
deploy/test-env/docker-compose.test.yml up -d qdrant --wait`` (or ``make test-integration-up``).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.lifecycle.coordinator import CustomIntentContext
from musubi.planes.artifact import ArtifactPlane
from musubi.planes.artifact.indexer import ArtifactIndexer
from musubi.store import bootstrap
from musubi.types.artifact import SourceArtifact
from musubi.types.common import generate_ksuid, utc_now

_CONTENT = "alpha beta gamma delta epsilon " * 200


@pytest.fixture
def real_qdrant() -> Iterator[QdrantClient]:
    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    client = QdrantClient(host="localhost", port=port)
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


def _artifact() -> SourceArtifact:
    now = utc_now()
    return SourceArtifact(
        object_id=generate_ksuid(),
        namespace="eric/dev/artifact",
        created_at=now,
        updated_at=now,
        title="race",
        filename="race.md",
        sha256="a" * 64,
        content_type="text/markdown",
        size_bytes=len(_CONTENT.encode()),
        chunker="token-sliding-v1",
    )


@pytest.mark.integration
def test_concurrent_same_artifact_index_single_committed_generation(
    real_qdrant: QdrantClient, tmp_path: Path
) -> None:
    """Invariant #5: N publishers racing the SAME artifact on real Qdrant resolve to exactly ONE
    committed generation — never a mixed-generation exposure — and the head chunk_count equals the
    visible committed count (#8). At least one attempt confirms; the losers fence (owner/generation-
    scoped cleanup)."""
    plane = ArtifactPlane(client=real_qdrant, embedder=FakeEmbedder())
    art = asyncio.run(plane.create(_artifact()))
    blob = tmp_path / art.namespace / art.object_id
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(_CONTENT.encode())

    indexer = ArtifactIndexer(client=real_qdrant, embedder=FakeEmbedder(), blob_root=tmp_path)
    n = 6
    ctxs = [
        CustomIntentContext(
            operation_key="opk-race",
            object_id=art.object_id,
            collection="musubi_artifact",
            namespace=art.namespace,
            owner_token=f"owner-{i}",  # each attempt's NEVER-REUSED owner
        )
        for i in range(n)
    ]
    barrier = threading.Barrier(n)

    def race(ctx: CustomIntentContext) -> str:
        barrier.wait()  # release all publishers together to force overlap at publication_version=0
        return indexer.apply(ctx)

    with ThreadPoolExecutor(max_workers=n) as ex:
        outcomes = list(ex.map(race, ctxs))

    assert outcomes.count("confirmed") >= 1
    assert set(outcomes) <= {"confirmed", "fence", "retry"}

    head = asyncio.run(plane.get(namespace=art.namespace, object_id=art.object_id))
    assert head is not None and head.artifact_state == "indexed"
    assert head.committed_generation and head.committed_owner

    chunks = asyncio.run(plane.chunks_for(namespace=art.namespace, object_id=art.object_id))
    assert len(chunks) == head.chunk_count  # #8: head count == visible committed count
    assert {c.generation for c in chunks} == {
        head.committed_generation
    }  # #5: ONE generation, no mix
    assert {c.owner_token for c in chunks} == {head.committed_owner}
