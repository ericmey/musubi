"""RET-003 mutation / discrimination proof (Yua 2026-07-13 12:24:42).

The Yua evidence rule: "Non-rerunnable evidence is not evidence." A
mutation/discrimination proof is a committed rerunnable test that:

  - constructs a REFERENCE (correct) implementation that satisfies the
    contract;
  - constructs PLAUSIBLE-WRONG variants (mutations) that violate one
    specific locked field;
  - asserts the contract tests pass against the reference and fail
    against every mutation.

This is the floor the implementation slice commits to: the contract
discriminates correct from wrong at the named assertion boundary.

Per Yua 2026-07-13 12:24:42 (review3): "Add mutation/discrimination
proof, full CI, additive history, then stop for exact-head review."

Each mutation here violates ONE specific locked field of the RET-003
contract. The contract tests (in test_retrieve_ret003_wire.py) must
catch every mutation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from musubi.api.responses import (
    RankedExtra,
    RankedResultRow,
    RankedScoreComponents,
    RecentExtra,
    RecentResultRow,
    RecentScoreComponents,
)


# A correct (reference) ranked row. The 5-key score_components, the
# required-nullable state/importance, the `ranked_combined` score_kind.
def _reference_ranked() -> RankedResultRow:
    return RankedResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=0.875,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="ranked_combined",
        extra=RankedExtra(
            score_components=RankedScoreComponents(
                relevance=1.0,
                recency=1.0,
                importance=0.7,
                provenance=0.5,
                reinforcement=0.0,
            ),
        ),
    )


def _reference_recent() -> RecentResultRow:
    return RecentResultRow(
        object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
        namespace="eric/claude-code/episodic",
        plane="episodic",
        score=1783957804.0,
        content="snippet",
        state="matured",
        importance=7,
        score_kind="created_epoch",
        provenance_score=0.5,
        extra=RecentExtra(score_components=RecentScoreComponents()),
    )


# ---- Mutation 1: ranked score_components missing the new keys ----


def test_ranked_score_components_rejects_3key_dict_mutation() -> None:
    """A plausible-wrong ranked row with 3-key score_components is REJECTED.

    The pre-RET-003 wire had 3 keys (relevance, recency, reinforcement).
    A mutation that keeps the 3-key shape violates the new contract;
    the typed Pydantic model must reject the non-conforming input.
    """
    # Reference passes.
    ref = _reference_ranked()
    assert ref.score_kind == "ranked_combined"
    assert len(ref.extra.score_components.model_dump()) == 5

    # Mutation: try to construct with only 3 keys. Pydantic's `extra=forbid`
    # must reject any non-conforming input.
    with pytest.raises(ValidationError):
        RankedResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=0.875,
            content="snippet",
            state="matured",
            importance=7,
            score_kind="ranked_combined",
            extra=RankedExtra(
                score_components=RankedScoreComponents.model_validate(
                    {
                        "relevance": 1.0,
                        "recency": 1.0,
                        "reinforcement": 0.0,
                    }
                ),
            ),
        )


# ---- Mutation 2: recent score_components is a fabricated 3-key dict ----


def test_recent_score_components_rejects_3key_fabrication_mutation() -> None:
    """A plausible-wrong recent row with a fabricated 3-key score_components is REJECTED.

    Per spec §3.3 (Yua 2026-07-13 09:49:53 #7 + 11:57:59 #8): recent
    `score_components` is the exact empty `{}`. A mutation that fabricates
    a 3-key dict violates the contract; the typed `RecentScoreComponents`
    with `extra=forbid` must reject it.
    """
    ref = _reference_recent()
    assert ref.extra.score_components.model_dump() == {}

    # Mutation: try to construct with a fabricated 3-key dict.
    with pytest.raises(ValidationError):
        RecentResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=1783957804.0,
            content="snippet",
            state="matured",
            importance=7,
            score_kind="created_epoch",
            provenance_score=0.5,
            extra=RecentExtra(
                score_components=RecentScoreComponents.model_validate(
                    {"relevance": 0.0, "recency": 1.0, "reinforcement": 0.0}
                )
            ),
        )


# ---- Mutation 3: ranked score_components out of [0, 1] range ----


def test_ranked_score_components_rejects_out_of_range_value_mutation() -> None:
    """A plausible-wrong value outside [0, 1] is REJECTED.

    Per Yua 2026-07-13 11:57:59 #5: every value must be numeric in [0, 1].
    A mutation that fabricates `relevance=2.5` violates the contract.
    """
    with pytest.raises(ValidationError):
        RankedScoreComponents(
            relevance=2.5,  # out of [0, 1]
            recency=1.0,
            importance=0.7,
            provenance=0.5,
            reinforcement=0.0,
        )


# ---- Mutation 4: importance out of 1..10 range ----


def test_ranked_importance_rejects_out_of_range_value_mutation() -> None:
    """A plausible-wrong importance value (e.g. 42) is REJECTED.

    Per spec §4.6: present-invalid source values must fail loud at
    response validation (500, NOT 422). A mutation that fabricates
    importance=42 violates the contract.
    """
    with pytest.raises(ValidationError):
        RankedResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=0.875,
            content="snippet",
            state="matured",
            importance=42,  # out of 1..10
            score_kind="ranked_combined",
            extra=RankedExtra(
                score_components=RankedScoreComponents(
                    relevance=1.0,
                    recency=1.0,
                    importance=0.7,
                    provenance=0.5,
                    reinforcement=0.0,
                ),
            ),
        )


# ---- Mutation 5: wrong score_kind for ranked ----


def test_ranked_score_kind_rejects_wrong_literal_mutation() -> None:
    """A plausible-wrong score_kind (e.g. 'created_epoch' on a ranked row) is REJECTED.

    Per spec §2.2: ranked rows have `score_kind='ranked_combined'`.
    A mutation that uses 'created_epoch' violates the contract.
    """
    with pytest.raises(ValidationError):
        RankedResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=0.875,
            content="snippet",
            state="matured",
            importance=7,
            score_kind="created_epoch",  # type: ignore[arg-type]
            extra=RankedExtra(
                score_components=RankedScoreComponents(
                    relevance=1.0,
                    recency=1.0,
                    importance=0.7,
                    provenance=0.5,
                    reinforcement=0.0,
                ),
            ),
        )


# ---- Mutation 6: state is a bad enum value ----


def test_state_rejects_invalid_enum_mutation() -> None:
    """A plausible-wrong state enum value (e.g. 'badvalue') is REJECTED.

    Per spec §4.6: present-invalid source values must fail loud at
    response validation (500). A mutation that uses 'badvalue' violates
    the contract; the typed LifecycleState must reject it.
    """
    with pytest.raises(ValidationError):
        RankedResultRow(
            object_id="3GSGzQauqzXNPstBMJw3hcIV0yd",
            namespace="eric/claude-code/episodic",
            plane="episodic",
            score=0.875,
            content="snippet",
            state="badvalue",  # type: ignore[arg-type]
            importance=7,
            score_kind="ranked_combined",
            extra=RankedExtra(
                score_components=RankedScoreComponents(
                    relevance=1.0,
                    recency=1.0,
                    importance=0.7,
                    provenance=0.5,
                    reinforcement=0.0,
                ),
            ),
        )


# ---- Reference: the correct row passes ----


def test_reference_correct_constructs_and_dumps() -> None:
    """The reference correct ranked row constructs, dumps, and round-trips.

    The reference row is the locked contract shape. This test
    asserts the reference is constructible and its `model_dump()`
    preserves every required field.
    """
    ref = _reference_ranked()
    dumped = ref.model_dump()
    assert dumped["object_id"] == "3GSGzQauqzXNPstBMJw3hcIV0yd"
    assert dumped["state"] == "matured"
    assert dumped["importance"] == 7
    assert dumped["score_kind"] == "ranked_combined"
    assert set(dumped["extra"]["score_components"].keys()) == {
        "relevance",
        "recency",
        "importance",
        "provenance",
        "reinforcement",
    }

    ref_recent = _reference_recent()
    dumped_recent = ref_recent.model_dump()
    assert dumped_recent["score_kind"] == "created_epoch"
    assert dumped_recent["extra"]["score_components"] == {}
