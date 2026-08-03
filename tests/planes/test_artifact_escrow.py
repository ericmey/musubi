"""ART-004 exact-byte stored-unindexed escrow contract."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient

from musubi.embedding import FakeEmbedder
from musubi.planes.artifact import ArtifactPlane
from musubi.store import bootstrap
from musubi.types.artifact import SourceArtifact
from musubi.types.common import Err, Ok, generate_ksuid, utc_now

_SOURCE_NAMESPACE = "yua/command-chair/episodic"
_SOURCE_OBJECT_ID = "3H1q1vkf5A6gJlMlUyyghHj65i0"
_ORIGINAL = b"original false claim\n"
_ORIGINAL_SHA256 = "e68a63298b26918d861ddc353b0ecac7e7bdc4150c6a6d7d5d57c450da7e5628"
_GOLDEN_ESCROW_ID = "3H1q1qtb3BGwodFekT1omv2IVZZ"


def _escrow_api() -> tuple[Any, Any, Any]:
    from musubi.planes.artifact.escrow import (
        ArtifactEscrowWriter,
        StoredUnindexedIndexingError,
        derive_escrow_address,
    )

    return ArtifactEscrowWriter, StoredUnindexedIndexingError, derive_escrow_address


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(":memory:")
    bootstrap(client)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def plane(qdrant: QdrantClient) -> ArtifactPlane:
    return ArtifactPlane(client=qdrant, embedder=FakeEmbedder())


def _address() -> Any:
    _, _, derive_escrow_address = _escrow_api()
    return derive_escrow_address(
        source_namespace=_SOURCE_NAMESPACE,
        source_object_id=_SOURCE_OBJECT_ID,
        original_sha256=_ORIGINAL_SHA256,
    )


def _writer(plane: ArtifactPlane, tmp_path: Path) -> Any:
    ArtifactEscrowWriter, _, _ = _escrow_api()
    return ArtifactEscrowWriter(plane=plane, blob_root=tmp_path)


def _blob_path(tmp_path: Path) -> Path:
    address = _address()
    return tmp_path / address.artifact_namespace / address.artifact_id


async def _store(writer: Any, content: bytes = _ORIGINAL) -> Any:
    return await writer.store(
        source_namespace=_SOURCE_NAMESPACE,
        source_object_id=_SOURCE_OBJECT_ID,
        original=content,
    )


def test_escrow_id_matches_adr_golden_vector() -> None:
    address = _address()

    assert hashlib.sha256(_ORIGINAL).hexdigest() == _ORIGINAL_SHA256
    assert address.artifact_namespace == "yua/command-chair/artifact"
    assert address.artifact_id == _GOLDEN_ESCROW_ID
    assert address.title == f"retracted-original-{_SOURCE_OBJECT_ID}-{_ORIGINAL_SHA256[:12]}"
    assert address.filename == f"{address.title}.txt"


def test_escrow_id_binds_namespace_source_and_digest_while_preserving_timestamp() -> None:
    from ksuid import Ksuid

    _, _, derive_escrow_address = _escrow_api()
    base = _address()
    changed_namespace = derive_escrow_address(
        source_namespace="aoi/command-chair/episodic",
        source_object_id=_SOURCE_OBJECT_ID,
        original_sha256=_ORIGINAL_SHA256,
    )
    changed_object = derive_escrow_address(
        source_namespace=_SOURCE_NAMESPACE,
        source_object_id="3H1q1vkf5A6gJlMlUyyghHj65i1",
        original_sha256=_ORIGINAL_SHA256,
    )
    changed_digest = derive_escrow_address(
        source_namespace=_SOURCE_NAMESPACE,
        source_object_id=_SOURCE_OBJECT_ID,
        original_sha256="0" * 64,
    )

    assert (
        len(
            {
                base.artifact_id,
                changed_namespace.artifact_id,
                changed_object.artifact_id,
                changed_digest.artifact_id,
            }
        )
        == 4
    )
    source_timestamp = bytes(Ksuid.from_base62(_SOURCE_OBJECT_ID))[:4]
    assert bytes(Ksuid.from_base62(base.artifact_id))[:4] == source_timestamp
    assert bytes(Ksuid.from_base62(changed_namespace.artifact_id))[:4] == source_timestamp
    assert bytes(Ksuid.from_base62(changed_digest.artifact_id))[:4] == source_timestamp


@pytest.mark.asyncio
async def test_escrow_temp_fsync_failure_exposes_no_final_or_head(
    plane: ArtifactPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    fsync_calls = 0
    complete_temp_seen = False

    def fail_first_file_fsync(fd: int) -> None:
        nonlocal fsync_calls, complete_temp_seen
        fsync_calls += 1
        if fsync_calls == 1:
            complete_temp_seen = os.fstat(fd).st_size == len(_ORIGINAL)
            raise OSError("injected temp fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_file_fsync)
    result = await _store(_writer(plane, tmp_path))

    assert fsync_calls == 1
    assert complete_temp_seen is True
    assert isinstance(result, Err)
    assert result.error.code == "blob_publish_failed"
    assert not _blob_path(tmp_path).exists()
    parent = _blob_path(tmp_path).parent
    assert not parent.exists() or [path for path in parent.iterdir() if path.suffix == ".tmp"] == []
    assert (
        await plane.get(namespace=_address().artifact_namespace, object_id=_address().artifact_id)
        is None
    )


@pytest.mark.asyncio
async def test_escrow_blob_readback_failure_exposes_no_head(
    plane: ArtifactPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = _blob_path(tmp_path)
    original_read_bytes = Path.read_bytes

    def corrupt_final_readback(path: Path) -> bytes:
        data = original_read_bytes(path)
        return b"corrupt readback" if path == final else data

    monkeypatch.setattr(Path, "read_bytes", corrupt_final_readback)
    result = await _store(_writer(plane, tmp_path))

    assert isinstance(result, Err)
    assert result.error.code == "blob_readback_mismatch"
    assert final.exists()
    assert original_read_bytes(final) == _ORIGINAL
    address = _address()
    assert (
        await plane.get(namespace=address.artifact_namespace, object_id=address.artifact_id) is None
    )


@pytest.mark.asyncio
async def test_escrow_head_failure_retry_reuses_verified_bytes_at_version_zero(
    plane: ArtifactPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_create = plane.create
    calls = 0

    async def fail_once(artifact: SourceArtifact) -> SourceArtifact:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected head publication failure")
        return await real_create(artifact)

    monkeypatch.setattr(plane, "create", fail_once)
    writer = _writer(plane, tmp_path)
    first = await _store(writer)
    final = _blob_path(tmp_path)
    first_inode = final.stat().st_ino

    assert isinstance(first, Err)
    assert first.error.code == "head_publish_failed"
    assert calls == 1
    assert final.read_bytes() == _ORIGINAL
    assert (
        await plane.get(namespace=_address().artifact_namespace, object_id=_address().artifact_id)
        is None
    )

    second = await _store(writer)
    assert isinstance(second, Ok)
    assert second.value.publication_version == 0
    assert final.stat().st_ino == first_inode
    assert calls == 2


@pytest.mark.asyncio
async def test_escrow_corrupt_final_blob_fails_closed_without_overwrite(
    plane: ArtifactPlane, tmp_path: Path
) -> None:
    final = _blob_path(tmp_path)
    final.parent.mkdir(parents=True)
    final.write_bytes(b"truncated")
    before_inode = final.stat().st_ino

    result = await _store(_writer(plane, tmp_path))

    assert isinstance(result, Err)
    assert result.error.code == "blob_mismatch"
    assert final.read_bytes() == b"truncated"
    assert final.stat().st_ino == before_inode
    assert (
        await plane.get(namespace=_address().artifact_namespace, object_id=_address().artifact_id)
        is None
    )


@pytest.mark.asyncio
async def test_concurrent_identical_escrows_converge_on_one_blob_and_head(
    plane: ArtifactPlane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    winner_injected = False

    def inject_concurrent_winner(src: Path, dst: Path) -> None:
        nonlocal winner_injected
        if not winner_injected:
            winner_injected = True
            real_link(src, dst)
        real_link(src, dst)

    monkeypatch.setattr(os, "link", inject_concurrent_winner)
    writer = _writer(plane, tmp_path)
    left = await _store(writer)
    right = await _store(writer)

    assert winner_injected is True
    assert isinstance(left, Ok)
    assert isinstance(right, Ok)
    assert left.value == right.value
    assert left.value.publication_version == 0
    assert _blob_path(tmp_path).read_bytes() == _ORIGINAL
    assert [path for path in _blob_path(tmp_path).parent.iterdir() if path.suffix == ".tmp"] == []


@pytest.mark.asyncio
async def test_verified_escrow_is_readable_with_zero_chunks_and_no_intent(
    plane: ArtifactPlane, tmp_path: Path
) -> None:
    result = await _store(_writer(plane, tmp_path))

    assert isinstance(result, Ok)
    artifact = result.value
    assert artifact.artifact_state == "stored_unindexed"
    assert artifact.chunk_count == 0
    assert artifact.committed_generation is None
    assert artifact.committed_owner is None
    assert artifact.index_operation_id is None
    assert artifact.publication_version == 0
    assert await plane.get(namespace=artifact.namespace, object_id=artifact.object_id) == artifact
    assert await plane.chunks_for(namespace=artifact.namespace, object_id=artifact.object_id) == []


@pytest.mark.asyncio
async def test_escrow_exact_text_search_misses_with_indexed_positive_control(
    plane: ArtifactPlane, tmp_path: Path
) -> None:
    result = await _store(_writer(plane, tmp_path))
    assert isinstance(result, Ok)
    escrow = result.value
    now = utc_now()
    normal = SourceArtifact(
        object_id=generate_ksuid(),
        namespace=escrow.namespace,
        title="search positive control",
        filename="search-positive.txt",
        sha256=hashlib.sha256(_ORIGINAL).hexdigest(),
        content_type="text/plain",
        size_bytes=len(_ORIGINAL),
        chunker="token-sliding-v1",
        created_at=now,
        updated_at=now,
    )
    await plane.create(normal)
    indexed = await plane.index(normal, _ORIGINAL.decode("utf-8"))
    assert indexed.artifact_state == "indexed"

    hits = await plane.query(namespace=escrow.namespace, query=_ORIGINAL.decode("utf-8"))
    assert any(hit.artifact_id == normal.object_id for hit in hits)
    assert all(hit.artifact_id != escrow.object_id for hit in hits)


@pytest.mark.asyncio
async def test_legacy_index_door_refuses_live_stored_head_from_stale_caller(
    plane: ArtifactPlane, tmp_path: Path
) -> None:
    _, StoredUnindexedIndexingError, _ = _escrow_api()
    result = await _store(_writer(plane, tmp_path))
    assert isinstance(result, Ok)
    stored = result.value
    stale_caller = stored.model_copy(update={"artifact_state": "indexing"})

    with pytest.raises(StoredUnindexedIndexingError, match="stored_unindexed"):
        await plane.index(stale_caller, _ORIGINAL.decode("utf-8"))

    assert await plane.get(namespace=stored.namespace, object_id=stored.object_id) == stored
    assert await plane.chunks_for(namespace=stored.namespace, object_id=stored.object_id) == []
