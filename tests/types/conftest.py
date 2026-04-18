"""Fixtures for ``tests/types/`` — the slice-types test suite."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from musubi.types import (
    ArtifactChunk,
    ArtifactRef,
    CuratedKnowledge,
    EpisodicMemory,
    SourceArtifact,
    SynthesizedConcept,
    Thought,
    generate_ksuid,
)


@pytest.fixture
def fixed_now() -> datetime:
    """A pinned UTC instant for deterministic tests."""
    return datetime(2026, 4, 17, 14, 23, 0, tzinfo=UTC)


@pytest.fixture
def episodic_namespace() -> str:
    return "eric/claude-code/episodic"


@pytest.fixture
def curated_namespace() -> str:
    return "eric/obsidian/curated"


@pytest.fixture
def concept_namespace() -> str:
    return "eric/synth/concept"


@pytest.fixture
def artifact_namespace() -> str:
    return "eric/uploads/artifact"


@pytest.fixture
def thought_namespace() -> str:
    return "eric/yua/thought"


@pytest.fixture
def sample_episodic(episodic_namespace: str) -> EpisodicMemory:
    return EpisodicMemory(
        namespace=episodic_namespace,
        content="user said hello",
        tags=["greeting"],
        source_context="Claude Code session test",
    )


@pytest.fixture
def sample_curated(curated_namespace: str) -> CuratedKnowledge:
    return CuratedKnowledge(
        namespace=curated_namespace,
        content="# GPU notes\n\nThe box has a 3080 with 10 GB VRAM.",
        title="GPU notes",
        vault_path="08-deployment/host-profile.md",
        body_hash="a" * 64,
        topics=["gpu", "deployment"],
    )


@pytest.fixture
def sample_concept(concept_namespace: str) -> SynthesizedConcept:
    return SynthesizedConcept(
        namespace=concept_namespace,
        content="Topic X seems to matter across 3 recent sessions.",
        title="Emerging topic X",
        synthesis_rationale="three sessions referenced X within a week",
        merged_from=[generate_ksuid(), generate_ksuid(), generate_ksuid()],
    )


@pytest.fixture
def sample_artifact(artifact_namespace: str) -> SourceArtifact:
    return SourceArtifact(
        namespace=artifact_namespace,
        title="design-notes.pdf",
        filename="design-notes.pdf",
        sha256="b" * 64,
        content_type="application/pdf",
        size_bytes=4096,
        chunker="markdown-headings-v1",
    )


@pytest.fixture
def sample_chunk() -> ArtifactChunk:
    return ArtifactChunk(
        chunk_id=generate_ksuid(),
        artifact_id=generate_ksuid(),
        chunk_index=0,
        content="First section of the doc.",
        start_offset=0,
        end_offset=25,
    )


@pytest.fixture
def sample_thought(thought_namespace: str) -> Thought:
    return Thought(
        namespace=thought_namespace,
        content="reminder: check GPU temps",
        from_presence="yua",
        to_presence="eric",
        channel="ops-alerts",
    )


@pytest.fixture
def sample_artifact_ref() -> ArtifactRef:
    return ArtifactRef(
        artifact_id=generate_ksuid(),
        chunk_id=generate_ksuid(),
        quote="the relevant excerpt",
    )
