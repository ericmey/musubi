"""Tests for ``EpisodicMemory``."""

from __future__ import annotations

from datetime import datetime

import pytest

from musubi.types import EpisodicMemory


def test_defaults_to_provisional(sample_episodic: EpisodicMemory) -> None:
    assert sample_episodic.state == "provisional"


def test_rejects_foreign_state(episodic_namespace: str) -> None:
    # Episodic can't be "promoted" (concept-only) or "synthesized".
    with pytest.raises(ValueError):
        EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            state="synthesized",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            state="promoted",  # type: ignore[arg-type]
        )


def test_content_required_non_empty(episodic_namespace: str) -> None:
    with pytest.raises(ValueError):
        EpisodicMemory(namespace=episodic_namespace, content="")


def test_event_at_tz_enforced(episodic_namespace: str) -> None:
    naive = datetime(2026, 4, 17, 14, 23)
    with pytest.raises(ValueError, match="timezone-aware"):
        EpisodicMemory(namespace=episodic_namespace, content="x", event_at=naive)


def test_modality_default_is_text(sample_episodic: EpisodicMemory) -> None:
    assert sample_episodic.modality == "text"


def test_modality_constrained_to_known_set(episodic_namespace: str) -> None:
    with pytest.raises(ValueError):
        EpisodicMemory(
            namespace=episodic_namespace,
            content="x",
            modality="video",  # type: ignore[arg-type]
        )


def test_roundtrip_json(sample_episodic: EpisodicMemory) -> None:
    restored = EpisodicMemory.model_validate_json(sample_episodic.model_dump_json())
    assert restored == sample_episodic
