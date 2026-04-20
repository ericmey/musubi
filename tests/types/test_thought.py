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


def test_thought_in_reply_to_accepts_ksuid(thought_namespace: str) -> None:
    from musubi.types.common import generate_ksuid

    k = generate_ksuid()
    t = Thought(
        namespace=thought_namespace, content="h", from_presence="a", to_presence="b", in_reply_to=k
    )
    assert t.in_reply_to == k


def test_thought_supersedes_accepts_ksuid_list(thought_namespace: str) -> None:
    from musubi.types.common import generate_ksuid

    k1, k2 = generate_ksuid(), generate_ksuid()
    t = Thought(
        namespace=thought_namespace,
        content="h",
        from_presence="a",
        to_presence="b",
        supersedes=[k1, k2],
    )
    assert t.supersedes == [k1, k2]
