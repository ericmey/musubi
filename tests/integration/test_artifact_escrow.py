"""ART-004 real-filesystem + real-Qdrant escrow ordering proof."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.artifact import ArtifactPlane
from musubi.store import bootstrap
from musubi.types.common import Ok, generate_ksuid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_storage_escrow_orders_verified_blob_before_head_and_reuses(
    tmp_path: Path,
) -> None:
    from musubi.planes.artifact.escrow import ArtifactEscrowWriter, derive_escrow_address

    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    qdrant = QdrantClient(host="127.0.0.1", port=port)
    try:
        bootstrap(qdrant)
        plane = ArtifactPlane(client=qdrant, embedder=FakeEmbedder())
        original = b"ART-004 real storage exact bytes\n"
        source_object_id = generate_ksuid()
        source_namespace = "eric/integration-test/episodic"
        address = derive_escrow_address(
            source_namespace=source_namespace,
            source_object_id=source_object_id,
            original_sha256=hashlib.sha256(original).hexdigest(),
        )
        writer = ArtifactEscrowWriter(plane=plane, blob_root=tmp_path)

        first = await writer.store(
            source_namespace=source_namespace,
            source_object_id=source_object_id,
            original=original,
        )
        assert isinstance(first, Ok)
        final = tmp_path / address.artifact_namespace / address.artifact_id
        assert final.read_bytes() == original
        assert hashlib.sha256(final.read_bytes()).hexdigest() == first.value.sha256
        assert (
            await plane.get(namespace=address.artifact_namespace, object_id=address.artifact_id)
            == first.value
        )
        first_inode = final.stat().st_ino

        second = await writer.store(
            source_namespace=source_namespace,
            source_object_id=source_object_id,
            original=original,
        )
        assert isinstance(second, Ok)
        assert second.value == first.value
        assert final.stat().st_ino == first_inode
        assert (
            await plane.chunks_for(
                namespace=address.artifact_namespace, object_id=address.artifact_id
            )
            == []
        )
    finally:
        qdrant.close()
