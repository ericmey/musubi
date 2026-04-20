"""Tests for ``SynthesizedConcept``."""

from __future__ import annotations

import pytest

from musubi.types import SynthesizedConcept, generate_ksuid, utc_now


def test_defaults_to_synthesized(sample_concept: SynthesizedConcept) -> None:
    assert sample_concept.state == "synthesized"


def test_merged_from_populated(sample_concept: SynthesizedConcept) -> None:
    assert len(sample_concept.merged_from) == 3


def test_promoted_state_requires_promoted_fields(concept_namespace: str) -> None:
    with pytest.raises(ValueError, match="promoted_to and promoted_at"):
        SynthesizedConcept(
            namespace=concept_namespace,
            content="x",
            title="T",
            synthesis_rationale="r",
            state="promoted",
        )


def test_promoted_to_without_promoted_at_rejected(
    concept_namespace: str,
) -> None:
    with pytest.raises(ValueError, match="promoted_at missing"):
        SynthesizedConcept(
            namespace=concept_namespace,
            content="x",
            title="T",
            synthesis_rationale="r",
            promoted_to=generate_ksuid(),
        )


def test_rejected_promotion_requires_reason(concept_namespace: str) -> None:
    with pytest.raises(ValueError, match="promotion_rejected_reason"):
        SynthesizedConcept(
            namespace=concept_namespace,
            content="x",
            title="T",
            synthesis_rationale="r",
            promotion_rejected_at=utc_now(),
        )


def test_roundtrip_json(sample_concept: SynthesizedConcept) -> None:
    restored = SynthesizedConcept.model_validate_json(sample_concept.model_dump_json())
    assert restored == sample_concept


def test_concept_topics_field_accepts_list(concept_namespace: str) -> None:
    c = SynthesizedConcept(
        namespace=concept_namespace,
        title="t",
        synthesis_rationale="r",
        content="c",
        merged_from=[generate_ksuid()],
        topics=["a"],
    )
    assert c.topics == ["a"]


def test_concept_promotion_attempts_default_zero(concept_namespace: str) -> None:
    c = SynthesizedConcept(
        namespace=concept_namespace,
        title="t",
        synthesis_rationale="r",
        content="c",
        merged_from=[generate_ksuid()],
    )
    assert c.promotion_attempts == 0


def test_concept_promotion_attempts_rejects_negative(concept_namespace: str) -> None:
    import pytest

    with pytest.raises(ValueError):
        SynthesizedConcept(
            namespace=concept_namespace,
            title="t",
            synthesis_rationale="r",
            content="c",
            merged_from=[generate_ksuid()],
            promotion_attempts=-1,
        )


def test_concept_last_reinforced_at_accepts_utc_datetime(concept_namespace: str) -> None:
    from datetime import datetime

    import pytest

    from musubi.types.common import utc_now

    now = utc_now()
    c = SynthesizedConcept(
        namespace=concept_namespace,
        title="t",
        synthesis_rationale="r",
        content="c",
        merged_from=[generate_ksuid()],
        last_reinforced_at=now,
    )
    assert c.last_reinforced_at is not None
    assert c.last_reinforced_epoch is not None

    naive = datetime(2026, 4, 17, 14, 23)
    with pytest.raises(ValueError, match="timezone-aware"):
        SynthesizedConcept(
            namespace=concept_namespace,
            title="t",
            synthesis_rationale="r",
            content="c",
            merged_from=[generate_ksuid()],
            last_reinforced_at=naive,
        )
