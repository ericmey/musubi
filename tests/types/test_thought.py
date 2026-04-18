"""Tests for ``Thought``."""

from __future__ import annotations

import pytest

from musubi.types import Thought


def test_defaults_to_provisional_unread(sample_thought: Thought) -> None:
    assert sample_thought.state == "provisional"
    assert sample_thought.read is False
    assert sample_thought.read_by == []


def test_channel_default(thought_namespace: str) -> None:
    t = Thought(
        namespace=thought_namespace,
        content="hi",
        from_presence="yua",
        to_presence="eric",
    )
    assert t.channel == "default"


def test_importance_bounded(thought_namespace: str) -> None:
    with pytest.raises(ValueError):
        Thought(
            namespace=thought_namespace,
            content="hi",
            from_presence="yua",
            to_presence="eric",
            importance=0,
        )


def test_to_presence_all_supported(thought_namespace: str) -> None:
    t = Thought(
        namespace=thought_namespace,
        content="broadcast",
        from_presence="yua",
        to_presence="all",
    )
    assert t.to_presence == "all"


def test_roundtrip_json(sample_thought: Thought) -> None:
    restored = Thought.model_validate_json(sample_thought.model_dump_json())
    assert restored == sample_thought
