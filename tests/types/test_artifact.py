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
