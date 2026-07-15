"""Deterministic retrieval scoring model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
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
    # RET-012: the seam at the cross-plane merge re-anchors each hit's
    # relevance against a single working-set global max, so the
    # per-leg-scored ``score_components["relevance"]`` (computed against
    # a local batch max) is no longer load-bearing for cross-plane
    # ranking. The two raw inputs are propagated here so the seam
    # (``calibrate_global_relevance``) can recompute relevance from the
    # canonical source. Internal-only — never projected onto wire models.
    raw_rrf_score: float | None = None
    raw_rerank_score: float | None = None


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
        # RET-012: propagate the raw relevance inputs so the cross-plane
        # seam can re-anchor against the working-set global max. Both are
        # intrinsic to the hit (the RRF from the fused hybrid search, the
        # cross-encoder logit from the reranker) and survive unchanged
        # through the per-leg scoring path.
        raw_rrf_score=hit.rrf_score,
        raw_rerank_score=hit.rerank_score,
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


# --------------------------------------------------------------------------- #
# RET-012 — cross-plane global relevance calibration
# --------------------------------------------------------------------------- #


def calibrate_global_relevance(
    candidates: list[Any],
) -> list[Any]:
    """Re-anchor each candidate's relevance against a single working-set global max.

    Per Issue #512, cross-plane fanout previously normalized relevance against
    each leg's local batch maximum, so a weak plane's sole hit would reach
    relevance 1.0 and outrank a materially stronger hit from another plane.
    This function is the seam at the cross-plane merge: it runs over the FULL
    pre-dedup candidate list, recomputes each hit's ``relevance`` and
    ``score`` against a single intrinsic quantity (``max(raw_rrf_score)``
    across the working set), then the existing ``best_by_id`` dedup picks
    the highest-recalibrated copy per ``object_id``.

    Three branches per candidate, decided by the raw inputs the per-leg
    scoring path preserved:

    1. ``raw_rerank_score is not None`` — the leg was reranked (deep /
       blended). The cross-encoder sigmoid is intrinsic and the seam
       preserves it (``relevance = _sigmoid(raw_rerank_score)``).
    2. ``raw_rrf_score is not None`` — the leg is fast mode (no rerank).
       The seam divides by the working-set max, not the per-leg max
       (``relevance = rrf / global_max``).
    3. Neither — recent mode or a non-ranked leg. The candidate is
       passed through unchanged; recent's existing ``created_epoch``
       ordering survives.

    ``score`` and ``score_components`` are recomputed from the new
    relevance plus the existing intrinsic components
    (``recency``/``importance``/``provenance``/``reinforcement``) so
    the cross-plane merge sorts on a globally comparable weighted total.
    The seam is intrinsic: the only input is the working set itself; no
    corpus scan, no hand-picked weight, no per-plane calibration table.

    The seam takes the duck-typed shape (any object with the fields the
    seam reads) rather than importing ``RetrievalResult`` to avoid a
    circular import with ``orchestration``. The cross-plane call site
    in ``orchestration._retrieve_uncounted`` passes
    ``list[RetrievalResult]``; tests in ``tests/retrieve/`` can pass
    any matching object that exposes the seam's required shape:

      - ``raw_rrf_score`` and ``raw_rerank_score`` attributes
        (both ``float | None``)
      - ``score`` attribute (float; only re-derived candidates are
        modified; passthrough candidates keep their original ``score``)
      - ``score_components`` attribute, either a ``dict[str, float]``
        (the wire-side shape, used by ``RetrievalResult``) or any
        object exposing the ``recency``/``importance``/``provenance``/
        ``reinforce`` (note: ``reinforce``, not ``reinforcement``)
        attributes (the ``ScoreComponents`` dataclass shape)
      - ``model_copy(update=...)`` for pydantic candidates, OR
        ``dataclasses.replace(...)`` for dataclass candidates (the
        passthrough branch is the identity and does not require
        either)
    """
    raw_rrfs = [
        c.raw_rrf_score for c in candidates if getattr(c, "raw_rrf_score", None) is not None
    ]
    global_max = max(raw_rrfs) if raw_rrfs else 1.0
    if global_max <= 0.0:
        global_max = 1.0

    out: list[Any] = []
    for c in candidates:
        rerank = getattr(c, "raw_rerank_score", None)
        rrf = getattr(c, "raw_rrf_score", None)
        if rerank is not None:
            new_relevance = _sigmoid(rerank)
        elif rrf is not None:
            new_relevance = _clamp01(rrf / global_max)
        else:
            # Recent mode (or any non-ranked leg). The leg's existing
            # score and score_components are the canonical ordering
            # signal; the seam must not touch them.
            out.append(c)
            continue

        comp = c.score_components
        if isinstance(comp, dict):
            recency = float(comp.get("recency", 0.0))
            importance = float(comp.get("importance", 0.0))
            provenance = float(comp.get("provenance", 0.0))
            reinforce = float(comp.get("reinforcement", 0.0))
        else:
            recency = float(getattr(comp, "recency", 0.0))
            importance = float(getattr(comp, "importance", 0.0))
            provenance = float(getattr(comp, "provenance", 0.0))
            reinforce = float(getattr(comp, "reinforce", 0.0))
        new_score = SCORE_WEIGHTS.combine(
            relevance=new_relevance,
            recency=recency,
            importance=importance,
            provenance=provenance,
            reinforce=reinforce,
        )
        if isinstance(comp, dict):
            new_components: Any = {
                "relevance": new_relevance,
                "recency": recency,
                "importance": importance,
                "provenance": provenance,
                "reinforcement": reinforce,
            }
        else:
            new_components = ScoreComponents(
                relevance=new_relevance,
                recency=recency,
                importance=importance,
                provenance=provenance,
                reinforce=reinforce,
            )
        if hasattr(c, "model_copy"):
            out.append(
                c.model_copy(update={"score": new_score, "score_components": new_components})
            )
        else:
            out.append(
                replace(
                    c,
                    score=new_score,
                    score_components=new_components,
                )
            )
    return out


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
    "calibrate_global_relevance",
    "rank_hits",
    "score",
    "score_result",
]
