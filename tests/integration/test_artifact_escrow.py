"""ART-004 real-filesystem + real-Qdrant escrow ordering proof."""

from __future__ import annotations

import hashlib
import os
import shutil
from contextlib import suppress
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.artifact import ArtifactPlane
from musubi.store import bootstrap
from musubi.types.common import Ok, generate_ksuid


def _configured_test_blob_root() -> Path:
    env_file = Path(__file__).resolve().parents[2] / "deploy" / "test-env" / ".env.test"
    prefix = "ARTIFACT_BLOB_PATH="
    matches = [
        line.removeprefix(prefix)
        for line in env_file.read_text().splitlines()
        if line.startswith(prefix)
    ]
    assert len(matches) == 1
    return Path(matches[0])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_storage_escrow_orders_verified_blob_before_head_and_reuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from musubi.planes.artifact.escrow import ArtifactEscrowWriter, derive_escrow_address

    port = int(os.environ.get("MUSUBI_TEST_QDRANT_PORT", "6339"))
    qdrant = QdrantClient(host="127.0.0.1", port=port)
    configured_root = _configured_test_blob_root()
    configured_root.mkdir(parents=True, exist_ok=True)
    blob_root = configured_root / f"art004-{generate_ksuid()}"
    blob_root.mkdir()
    assert blob_root.stat().st_dev == configured_root.stat().st_dev
    real_link = os.link
    winner_injected = False

    def inject_concurrent_winner(src: Path, dst: Path) -> None:
        nonlocal winner_injected
        if not winner_injected:
            winner_injected = True
            real_link(src, dst)
        real_link(src, dst)

    monkeypatch.setattr(os, "link", inject_concurrent_winner)
    plane: ArtifactPlane | None = None
    cleanup_identity: tuple[str, str] | None = None
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
        cleanup_identity = (address.artifact_namespace, address.artifact_id)
        writer = ArtifactEscrowWriter(plane=plane, blob_root=blob_root)

        first = await writer.store(
            source_namespace=source_namespace,
            source_object_id=source_object_id,
            original=original,
        )
        assert winner_injected is True
        assert isinstance(first, Ok)
        final = blob_root / address.artifact_namespace / address.artifact_id
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
        shutil.rmtree(blob_root, ignore_errors=True)
        if plane is not None and cleanup_identity is not None:
            with suppress(Exception):
                await plane.purge(
                    namespace=cleanup_identity[0],
                    object_id=cleanup_identity[1],
                )
        with suppress(Exception):
            qdrant.close()
