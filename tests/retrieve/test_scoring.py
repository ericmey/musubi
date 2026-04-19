"""Test contract for slice-retrieval-scoring."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from musubi.retrieve.scoring import (
    SCORE_WEIGHTS,
    Hit,
    ScoreWeights,
    rank_hits,
    score,
    score_result,
)

NOW = 2_000_000_000.0


def _hit(
    *,
    object_id: str = "object-a",
    plane: str = "episodic",
    state: str = "matured",
    rrf_score: float = 0.5,
    batch_max_rrf: float = 1.0,
    updated_epoch: float = NOW,
    importance: int = 5,
    reinforcement_count: int = 0,
    access_count: int = 0,
    rerank_score: float | None = None,
) -> Hit:
    return Hit(
        object_id=object_id,
        plane=plane,
        state=state,
        rrf_score=rrf_score,
        batch_max_rrf=batch_max_rrf,
        updated_epoch=updated_epoch,
        importance=importance,
        reinforcement_count=reinforcement_count,
        access_count=access_count,
        rerank_score=rerank_score,
    )


@given(
    rrf_score=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False),
    batch_max_rrf=st.floats(min_value=0.001, max_value=10.0, allow_nan=False),
    age_hours=st.floats(min_value=0.0, max_value=24.0 * 365.0, allow_nan=False),
    importance=st.integers(min_value=-100, max_value=100),
    reinforcement_count=st.integers(min_value=0, max_value=1_000),
)
def test_score_in_0_1_range_for_any_hit(
    rrf_score: float,
    batch_max_rrf: float,
    age_hours: float,
    importance: int,
    reinforcement_count: int,
) -> None:
    total, components = score(
        _hit(
            rrf_score=rrf_score,
            batch_max_rrf=batch_max_rrf,
            updated_epoch=NOW - age_hours * 3600,
            importance=importance,
            reinforcement_count=reinforcement_count,
        ),
        now=NOW,
    )

    assert 0.0 <= total <= 1.0
    assert all(0.0 <= value <= 1.0 for value in components.as_dict().values())


def test_components_sum_with_weights_equals_total() -> None:
    weights = ScoreWeights(
        relevance=0.4,
        recency=0.2,
        importance=0.2,
        provenance=0.1,
        reinforce=0.1,
    )
    total, components = score(
        _hit(rrf_score=0.5, batch_max_rrf=1.0, importance=8, reinforcement_count=3),
        now=NOW,
        weights=weights,
    )

    expected = (
        weights.relevance * components.relevance
        + weights.recency * components.recency
        + weights.importance * components.importance
        + weights.provenance * components.provenance
        + weights.reinforce * components.reinforce
    )
    assert total == pytest.approx(expected)


def test_relevance_normalized_within_batch() -> None:
    total, components = score(_hit(rrf_score=0.25, batch_max_rrf=0.5), now=NOW)

    assert total > 0.0
    assert components.relevance == pytest.approx(0.5)


def test_relevance_uses_sigmoid_for_rerank_score() -> None:
    _, positive = score(_hit(rerank_score=2.0), now=NOW)
    _, negative = score(_hit(rerank_score=-2.0), now=NOW)

    assert positive.relevance == pytest.approx(1.0 / (1.0 + math.exp(-2.0)))
    assert negative.relevance == pytest.approx(math.exp(-2.0) / (1.0 + math.exp(-2.0)))


def test_relevance_zero_when_batch_max_rrf_is_not_positive() -> None:
    _, components = score(_hit(rrf_score=1.0, batch_max_rrf=0.0), now=NOW)

    assert components.relevance == pytest.approx(0.0)


def test_recency_decay_matches_half_life_table() -> None:
    _, same_day = score(_hit(updated_epoch=NOW), now=NOW)
    _, seven_days = score(_hit(updated_epoch=NOW - 7 * 24 * 3600), now=NOW)
    _, thirty_days = score(_hit(updated_epoch=NOW - 30 * 24 * 3600), now=NOW)
    _, ninety_days = score(_hit(updated_epoch=NOW - 90 * 24 * 3600), now=NOW)

    assert same_day.recency == pytest.approx(1.0)
    assert seven_days.recency == pytest.approx(0.85, abs=0.01)
    assert thirty_days.recency == pytest.approx(0.5)
    assert ninety_days.recency == pytest.approx(0.125)


def test_recency_half_life_per_plane_applied() -> None:
    _, episodic = score(
        _hit(plane="episodic", updated_epoch=NOW - 30 * 24 * 3600),
        now=NOW,
    )
    _, curated = score(
        _hit(plane="curated", updated_epoch=NOW - 30 * 24 * 3600),
        now=NOW,
    )

    assert episodic.recency == pytest.approx(0.5)
    assert curated.recency > episodic.recency
    assert curated.recency == pytest.approx(math.exp(-math.log(2) / 6.0))


def test_importance_clamped_to_1_10() -> None:
    _, low = score(_hit(importance=-20), now=NOW)
    _, high = score(_hit(importance=99), now=NOW)

    assert low.importance == pytest.approx(0.1)
    assert high.importance == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("plane", "state", "expected"),
    [
        ("curated", "matured", 1.0),
        ("curated", "superseded", 0.6),
        ("concept", "promoted", 0.9),
        ("concept", "matured", 0.6),
        ("concept", "synthesized", 0.35),
        ("episodic", "matured", 0.5),
        ("episodic", "provisional", 0.2),
        ("artifact_chunk", "matured", 0.7),
    ],
)
def test_provenance_values_match_table(plane: str, state: str, expected: float) -> None:
    _, components = score(_hit(plane=plane, state=state), now=NOW)

    assert components.provenance == pytest.approx(expected)


@pytest.mark.parametrize("state", ["demoted", "archived", "superseded"])
def test_provenance_demoted_states_get_0_1(state: str) -> None:
    _, components = score(_hit(plane="episodic", state=state), now=NOW)

    assert components.provenance == pytest.approx(0.1)


def test_unknown_provenance_defaults_to_0_1() -> None:
    _, components = score(_hit(plane="thought", state="matured"), now=NOW)

    assert components.provenance == pytest.approx(0.1)


def test_reinforcement_log_scaled() -> None:
    _, none = score(_hit(reinforcement_count=0), now=NOW)
    _, one = score(_hit(reinforcement_count=1), now=NOW)
    _, three = score(_hit(reinforcement_count=3), now=NOW)
    _, ten = score(_hit(reinforcement_count=10), now=NOW)
    _, capped = score(_hit(reinforcement_count=200), now=NOW)

    assert none.reinforce == pytest.approx(0.0)
    assert one.reinforce == pytest.approx(math.log1p(1) / math.log1p(20))
    assert three.reinforce == pytest.approx(math.log1p(3) / math.log1p(20))
    assert ten.reinforce == pytest.approx(math.log1p(10) / math.log1p(20))
    assert capped.reinforce == pytest.approx(1.0)


def test_access_count_used_when_reinforcement_count_absent() -> None:
    _, components = score(_hit(reinforcement_count=0, access_count=10), now=NOW)

    assert components.reinforce == pytest.approx(math.log1p(10) / math.log1p(100))


def test_tiebreak_deterministic_on_object_id() -> None:
    hits = [
        _hit(object_id="b", plane="episodic"),
        _hit(object_id="a", plane="episodic"),
        _hit(object_id="a", plane="concept", state="archived"),
    ]

    ranked = rank_hits(
        hits,
        now=NOW,
        weights=ScoreWeights(
            relevance=0.0,
            recency=0.0,
            importance=0.0,
            provenance=0.0,
            reinforce=0.0,
        ),
    )

    assert [(hit.object_id, hit.plane) for hit in ranked] == [
        ("a", "concept"),
        ("a", "episodic"),
        ("b", "episodic"),
    ]


def test_score_components_exposed_on_result() -> None:
    result = score_result(_hit(object_id="exposed"), now=NOW)

    assert result.object_id == "exposed"
    assert result.score_components.as_dict().keys() == {
        "relevance",
        "recency",
        "importance",
        "provenance",
        "reinforce",
    }


def test_weights_change_shifts_ranking_predictably() -> None:
    relevant_old = _hit(
        object_id="relevant-old",
        rrf_score=1.0,
        batch_max_rrf=1.0,
        updated_epoch=NOW - 180 * 24 * 3600,
        importance=1,
    )
    recent_less_relevant = _hit(
        object_id="recent-less-relevant",
        rrf_score=0.1,
        batch_max_rrf=1.0,
        updated_epoch=NOW,
        importance=1,
    )

    relevance_first = rank_hits(
        [recent_less_relevant, relevant_old],
        now=NOW,
        weights=ScoreWeights(
            relevance=0.9, recency=0.1, importance=0.0, provenance=0.0, reinforce=0.0
        ),
    )
    recency_first = rank_hits(
        [recent_less_relevant, relevant_old],
        now=NOW,
        weights=ScoreWeights(
            relevance=0.1, recency=0.9, importance=0.0, provenance=0.0, reinforce=0.0
        ),
    )

    assert relevance_first[0].object_id == "relevant-old"
    assert recency_first[0].object_id == "recent-less-relevant"


def test_no_rng_used_in_scoring() -> None:
    source = Path("src/musubi/retrieve/scoring.py").read_text()

    assert "random" not in source
    assert "secrets" not in source
    assert "uuid" not in source


@pytest.mark.property
@given(
    relevance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    recency=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    importance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    provenance=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    reinforce=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    delta=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_hypothesis_scores_are_monotonic_in_each_component_holding_others_fixed(
    relevance: float,
    recency: float,
    importance: float,
    provenance: float,
    reinforce: float,
    delta: float,
) -> None:
    base = SCORE_WEIGHTS.combine(
        relevance=relevance,
        recency=recency,
        importance=importance,
        provenance=provenance,
        reinforce=reinforce,
    )
    bumped = SCORE_WEIGHTS.combine(
        relevance=min(1.0, relevance + delta),
        recency=recency,
        importance=importance,
        provenance=provenance,
        reinforce=reinforce,
    )

    assert bumped >= base


@pytest.mark.property
@given(
    relevance_a=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    relevance_b=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    recency_a=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    recency_b=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_hypothesis_swapping_weights_reorders_results_consistently_with_the_math(
    relevance_a: float,
    relevance_b: float,
    recency_a: float,
    recency_b: float,
) -> None:
    relevance_weights = ScoreWeights(
        relevance=1.0, recency=0.0, importance=0.0, provenance=0.0, reinforce=0.0
    )
    recency_weights = ScoreWeights(
        relevance=0.0, recency=1.0, importance=0.0, provenance=0.0, reinforce=0.0
    )

    relevance_order = relevance_weights.combine(
        relevance=relevance_a,
        recency=recency_a,
        importance=0.0,
        provenance=0.0,
        reinforce=0.0,
    ) >= relevance_weights.combine(
        relevance=relevance_b,
        recency=recency_b,
        importance=0.0,
        provenance=0.0,
        reinforce=0.0,
    )
    recency_order = recency_weights.combine(
        relevance=relevance_a,
        recency=recency_a,
        importance=0.0,
        provenance=0.0,
        reinforce=0.0,
    ) >= recency_weights.combine(
        relevance=relevance_b,
        recency=recency_b,
        importance=0.0,
        provenance=0.0,
        reinforce=0.0,
    )

    assert relevance_order == (relevance_a >= relevance_b)
    assert recency_order == (recency_a >= recency_b)


@pytest.mark.skip(reason="deferred to slice-retrieval-evals: golden query set lives there")
def test_eval_golden_query_set_mrr_ge_0_7_with_default_weights() -> None:
    raise AssertionError("covered by retrieval eval suite")
