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
