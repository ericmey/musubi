"""Tests for ``SourceArtifact`` + ``ArtifactChunk``."""

from __future__ import annotations

import pytest

from musubi.types import ArtifactChunk, SourceArtifact, generate_ksuid


class TestSourceArtifact:
    def test_starts_in_indexing(self, sample_artifact: SourceArtifact) -> None:
        assert sample_artifact.artifact_state == "indexing"
        assert sample_artifact.state == "matured"

    def test_sha256_must_be_64_hex(self, artifact_namespace: str) -> None:
        with pytest.raises(ValueError):
            SourceArtifact(
                namespace=artifact_namespace,
                title="x",
                filename="x",
                sha256="nope",
                content_type="text/plain",
                size_bytes=0,
                chunker="c",
            )

    def test_failed_requires_reason(self, artifact_namespace: str) -> None:
        with pytest.raises(ValueError, match="failure_reason"):
            SourceArtifact(
                namespace=artifact_namespace,
                title="x",
                filename="x",
                sha256="a" * 64,
                content_type="text/plain",
                size_bytes=1,
                chunker="c",
                artifact_state="failed",
            )

    def test_indexed_requires_chunks(self, artifact_namespace: str) -> None:
        with pytest.raises(ValueError, match="chunk_count"):
            SourceArtifact(
                namespace=artifact_namespace,
                title="x",
                filename="x",
                sha256="a" * 64,
                content_type="text/plain",
                size_bytes=1,
                chunker="c",
                artifact_state="indexed",
                chunk_count=0,
            )

    def test_size_bytes_non_negative(self, artifact_namespace: str) -> None:
        with pytest.raises(ValueError):
            SourceArtifact(
                namespace=artifact_namespace,
                title="x",
                filename="x",
                sha256="a" * 64,
                content_type="text/plain",
                size_bytes=-1,
                chunker="c",
            )

    def test_roundtrip_json(self, sample_artifact: SourceArtifact) -> None:
        restored = SourceArtifact.model_validate_json(sample_artifact.model_dump_json())
        assert restored == sample_artifact


def test_stored_unindexed_accepts_only_empty_indexing_state(artifact_namespace: str) -> None:
    artifact = SourceArtifact(
        namespace=artifact_namespace,
        title="retracted-original-3Hexample",
        filename="retracted-original-3Hexample.txt",
        sha256="a" * 64,
        content_type="text/plain",
        size_bytes=32769,
        chunker="stored-unindexed-v1",
        artifact_state="stored_unindexed",
        chunk_count=0,
        committed_generation=None,
        committed_owner=None,
        index_operation_id=None,
        failure_reason=None,
    )

    assert artifact.artifact_state == "stored_unindexed"
    assert artifact.chunk_count == 0
    assert artifact.committed_generation is None
    assert artifact.committed_owner is None
    assert artifact.index_operation_id is None
    assert artifact.failure_reason is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_count", 1),
        ("committed_generation", "generation-1"),
        ("committed_owner", "owner-1"),
        ("index_operation_id", "operation-1"),
        ("failure_reason", "indexing failed"),
    ],
)
def test_stored_unindexed_rejects_every_indexing_owned_field(
    artifact_namespace: str, field: str, value: object
) -> None:
    data: dict[str, object] = {
        "namespace": artifact_namespace,
        "title": "retracted-original-3Hexample",
        "filename": "retracted-original-3Hexample.txt",
        "sha256": "a" * 64,
        "content_type": "text/plain",
        "size_bytes": 32769,
        "chunker": "stored-unindexed-v1",
        "artifact_state": "stored_unindexed",
    }
    data[field] = value

    with pytest.raises(ValueError, match=field):
        SourceArtifact.model_validate(data)


class TestArtifactChunk:
    def test_offsets_ordered(self) -> None:
        with pytest.raises(ValueError, match="end_offset"):
            ArtifactChunk(
                chunk_id=generate_ksuid(),
                artifact_id=generate_ksuid(),
                chunk_index=0,
                content="x",
                start_offset=100,
                end_offset=50,
            )

    def test_is_frozen(self, sample_chunk: ArtifactChunk) -> None:
        with pytest.raises(Exception):
            sample_chunk.content = "changed"

    def test_roundtrip_json(self, sample_chunk: ArtifactChunk) -> None:
        restored = ArtifactChunk.model_validate_json(sample_chunk.model_dump_json())
        assert restored == sample_chunk
