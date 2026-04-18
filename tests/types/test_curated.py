"""Tests for ``CuratedKnowledge``."""

from __future__ import annotations

from datetime import timedelta

import pytest

from musubi.types import CuratedKnowledge, generate_ksuid, utc_now


def test_defaults_to_matured(sample_curated: CuratedKnowledge) -> None:
    assert sample_curated.state == "matured"


def test_never_provisional(curated_namespace: str) -> None:
    with pytest.raises(ValueError):
        CuratedKnowledge(
            namespace=curated_namespace,
            content="x",
            title="T",
            vault_path="t.md",
            body_hash="a" * 64,
            state="provisional",  # type: ignore[arg-type]
        )


def test_body_hash_must_be_64_hex(curated_namespace: str) -> None:
    with pytest.raises(ValueError):
        CuratedKnowledge(
            namespace=curated_namespace,
            content="x",
            title="T",
            vault_path="t.md",
            body_hash="short",
        )
    with pytest.raises(ValueError):
        CuratedKnowledge(
            namespace=curated_namespace,
            content="x",
            title="T",
            vault_path="t.md",
            body_hash="Z" * 64,  # not hex
        )


def test_promotion_metadata_pair_enforced(curated_namespace: str) -> None:
    # promoted_from without promoted_at is invalid (promotion is one event).
    with pytest.raises(ValueError, match="promoted_at"):
        CuratedKnowledge(
            namespace=curated_namespace,
            content="x",
            title="T",
            vault_path="t.md",
            body_hash="a" * 64,
            promoted_from=generate_ksuid(),
        )


def test_valid_from_until_window(curated_namespace: str) -> None:
    start = utc_now()
    end = start + timedelta(days=30)
    obj = CuratedKnowledge(
        namespace=curated_namespace,
        content="x",
        title="T",
        vault_path="t.md",
        body_hash="a" * 64,
        valid_from=start,
        valid_until=end,
    )
    assert obj.valid_from_epoch == start.timestamp()
    assert obj.valid_until_epoch == end.timestamp()


def test_roundtrip_json(sample_curated: CuratedKnowledge) -> None:
    restored = CuratedKnowledge.model_validate_json(sample_curated.model_dump_json())
    assert restored == sample_curated
