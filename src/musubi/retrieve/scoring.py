"""Deterministic retrieval scoring model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from musubi.types.common import LifecycleState


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """Weights for the retrieval score components."""

    relevance: float = 0.55
    recency: float = 0.15
    importance: float = 0.10
    provenance: float = 0.15
    reinforce: float = 0.05

    def combine(
        self,
        *,
        relevance: float,
        recency: float,
        importance: float,
        provenance: float,
        reinforce: float,
    ) -> float:
        total = (
            self.relevance * _clamp01(relevance)
            + self.recency * _clamp01(recency)
            + self.importance * _clamp01(importance)
            + self.provenance * _clamp01(provenance)
            + self.reinforce * _clamp01(reinforce)
        )
        return _clamp01(total)


SCORE_WEIGHTS = ScoreWeights()


@dataclass(frozen=True, slots=True)
class ScoreComponents:
    """Named components that explain a retrieval score."""

    relevance: float
    recency: float
    importance: float
    provenance: float
    reinforce: float

    def as_dict(self) -> dict[str, float]:
        return {
            "relevance": self.relevance,
            "recency": self.recency,
            "importance": self.importance,
            "provenance": self.provenance,
            "reinforce": self.reinforce,
        }


@dataclass(frozen=True, slots=True)
class Hit:
    """Minimal retrieval hit shape accepted by the scoring model."""

    object_id: str
    plane: str
    state: str
    rrf_score: float
    batch_max_rrf: float
    updated_epoch: float
    importance: int = 5
    reinforcement_count: int = 0
    access_count: int = 0
    rerank_score: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScoredHit:
    """A scored retrieval hit with components exposed for debug/evals."""

    object_id: str
    plane: str
    state: str
    score: float
    score_components: ScoreComponents
    payload: dict[str, Any]


_RECENCY_HALF_LIFE_DAYS: dict[str, float] = {
    "curated": 180.0,
    "episodic": 30.0,
}
_DEFAULT_RECENCY_HALF_LIFE_DAYS = 30.0

_PROVENANCE: dict[tuple[str, str], float] = {
    ("curated", "matured"): 1.0,
    ("curated", "superseded"): 0.6,
    ("concept", "promoted"): 0.9,
    ("concept", "matured"): 0.6,
    ("concept", "synthesized"): 0.35,
    ("episodic", "matured"): 0.5,
    ("episodic", "provisional"): 0.2,
    ("artifact_chunk", "matured"): 0.7,
}
_LOW_PROVENANCE_STATES: set[LifecycleState] = {"demoted", "archived", "superseded"}


def score(
    hit: Hit,
    *,
    now: float,
    weights: ScoreWeights = SCORE_WEIGHTS,
) -> tuple[float, ScoreComponents]:
    """Return the weighted score and explainable components for one hit."""

    components = ScoreComponents(
        relevance=_relevance(hit),
        recency=_recency(hit, now=now),
        importance=_importance(hit),
        provenance=_provenance(hit),
        reinforce=_reinforcement(hit),
    )
    return (
        weights.combine(
            relevance=components.relevance,
            recency=components.recency,
            importance=components.importance,
            provenance=components.provenance,
            reinforce=components.reinforce,
        ),
        components,
    )


def score_result(
    hit: Hit,
    *,
    now: float,
    weights: ScoreWeights = SCORE_WEIGHTS,
) -> ScoredHit:
    """Return a scored hit suitable for API/eval exposure."""

    total, components = score(hit, now=now, weights=weights)
    return ScoredHit(
        object_id=hit.object_id,
        plane=hit.plane,
        state=hit.state,
        score=total,
        score_components=components,
        payload=dict(hit.payload),
    )


def rank_hits(
    hits: list[Hit],
    *,
    now: float,
    weights: ScoreWeights = SCORE_WEIGHTS,
) -> list[ScoredHit]:
    """Score and sort hits with deterministic tie-breaks."""

    scored = [score_result(hit, now=now, weights=weights) for hit in hits]
    return sorted(scored, key=lambda hit: (-hit.score, hit.object_id, hit.plane))


def _relevance(hit: Hit) -> float:
    if hit.rerank_score is not None:
        return _sigmoid(hit.rerank_score)
    if hit.batch_max_rrf <= 0.0:
        return 0.0
    return _clamp01(hit.rrf_score / hit.batch_max_rrf)


def _recency(hit: Hit, *, now: float) -> float:
    age_hours = max(0.0, (now - hit.updated_epoch) / 3600.0)
    half_life_days = _RECENCY_HALF_LIFE_DAYS.get(hit.plane, _DEFAULT_RECENCY_HALF_LIFE_DAYS)
    return math.exp(-age_hours * math.log(2.0) / (half_life_days * 24.0))


def _importance(hit: Hit) -> float:
    return min(10, max(1, hit.importance)) / 10.0


def _provenance(hit: Hit) -> float:
    table_value = _PROVENANCE.get((hit.plane, hit.state))
    if table_value is not None:
        return table_value
    if hit.state in _LOW_PROVENANCE_STATES:
        return 0.1
    return 0.1


def _reinforcement(hit: Hit) -> float:
    if hit.reinforcement_count > 0:
        return min(1.0, math.log1p(hit.reinforcement_count) / math.log1p(20))
    return min(1.0, math.log1p(hit.access_count) / math.log1p(100))


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


__all__ = [
    "SCORE_WEIGHTS",
    "Hit",
    "ScoreComponents",
    "ScoreWeights",
    "ScoredHit",
    "rank_hits",
    "score",
    "score_result",
]
