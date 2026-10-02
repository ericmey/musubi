---
title: Scoring Model
section: 05-retrieval
tags: [ranking, retrieval, scoring, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-02
up: "[[05-retrieval/index]]"
reviewed: false
implements: "tests/retrieve/test_scoring.py"
---
# Scoring Model

The single function that turns a raw retrieval hit into a rank-orderable number. Used by the fast and deep paths (and so by blended, which runs deep per plane). Lives in `src/musubi/retrieve/scoring.py`.

## The formula

```python
def score(
    hit: Hit,
    *,
    now: float,                          # unix epoch
    weights: ScoreWeights = SCORE_WEIGHTS,
) -> tuple[float, ScoreComponents]:
    relevance   = _relevance(hit)                              # 0..1
    recency     = _recency(hit, now)                           # 0..1
    importance  = _importance(hit)                             # 0..1
    provenance  = _provenance(hit)                             # 0..1
    reinforce   = _reinforcement(hit)                          # 0..1

    total = clamp01(
        weights.relevance   * relevance
      + weights.recency     * recency
      + weights.importance  * importance
      + weights.provenance  * provenance
      + weights.reinforce   * reinforce
    )

    return total, ScoreComponents(
        relevance=relevance,
        recency=recency,
        importance=importance,
        provenance=provenance,
        reinforce=reinforce,
    )
```

Default weights (the `ScoreWeights` dataclass defaults in `src/musubi/retrieve/scoring.py`; `score()` takes a `weights` argument, and there is no runtime setting for them):

```python
SCORE_WEIGHTS = ScoreWeights(
    relevance=0.55,
    recency=0.15,
    importance=0.10,
    provenance=0.15,
    reinforce=0.05,
)
```

Inspired by Stanford's "Generative Agents" retrieval formula ([https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442)), extended with provenance (memory type) and reinforcement (how many times the memory has been re-discovered). The formula is *not* an emotional-valence score — that's a separate, future add-on.

## Component definitions

### Relevance (0..1)

When the hit was reranked, relevance is the sigmoid of the cross-encoder score. Otherwise it is Qdrant's server-side RRF fusion score normalized by the batch maximum:

```python
def _relevance(hit: Hit) -> float:
    if hit.rerank_score is not None:
        return sigmoid(hit.rerank_score)
    if hit.batch_max_rrf <= 0.0:
        return 0.0
    return clamp01(hit.rrf_score / hit.batch_max_rrf)
```

When a request fans out across several plane targets, the orchestrator re-anchors relevance against the working-set maximum before merging ([[05-retrieval/cross-plane-ranking]]).

### Recency (0..1)

Exponential decay over age, in hours:

```python
def _recency(hit: Hit, now: float) -> float:
    age_hours = max(0.0, (now - hit.updated_epoch) / 3600)
    half_life_days = _RECENCY_HALF_LIFE_DAYS.get(hit.plane, 30.0)
    return math.exp(-age_hours * math.log(2) / (half_life_days * 24))
```

A 30-day half-life means:

- Same-day: ~1.0
- 7 days old: ~0.85
- 30 days old: 0.5
- 90 days old: 0.125
- 1 year old: ~0.0002

Half-life is per plane, from the module constant `_RECENCY_HALF_LIFE_DAYS`: curated 180 days (curated facts age slower, since they're generally more durable), episodic 30 days, and 30 days for any other plane. It is not an environment setting.

### Importance (0..1)

```python
def _importance(hit: Hit) -> float:
    return max(1, min(10, hit.importance)) / 10.0
```

Importance is an object-level field (1-10). The model default is 5 (`src/musubi/types/base.py`), and the retrieval paths treat a missing value as 5.

### Provenance (0..1)

A constant per (plane, state) pair, reflecting how trustworthy this memory type is:

| Plane | State | Provenance |
|---|---|---|
| curated | matured | 1.0 |
| curated | superseded | 0.6 |
| concept | promoted | 0.9 |
| concept | matured | 0.6 |
| concept | synthesized | 0.35 |
| episodic | matured | 0.5 |
| episodic | provisional | 0.2 |
| `artifact_chunk` | matured | 0.7 |

The table keys on the hit's `plane` string. Hits from the artifact collection currently carry `plane="artifact"`, not `artifact_chunk`, so they fall through to the default below rather than 0.7.

Any `(plane, state)` pair not in the table gets 0.1, including `demoted`, `archived`, and `superseded` states outside the table (they're rarely returned in the first place, but if filters let them through, they're demoted in score).

### Reinforcement (0..1)

Log-scaled count:

```python
def _reinforcement(hit: Hit) -> float:
    if hit.reinforcement_count > 0:
        return min(1.0, math.log1p(hit.reinforcement_count) / math.log1p(20))
    return min(1.0, math.log1p(hit.access_count) / math.log1p(100))
```

- 0 reinforcements → 0.0
- 1 → 0.23
- 3 → 0.45
- 10 → 0.79
- 20+ → 1.0

When `reinforcement_count` is zero or absent, `access_count` is used instead, log-scaled with a larger cap (100) — re-access is weaker evidence than re-synthesis.

## What isn't in the score (and why)

- **Per-user affinity / personalization.** Everything already filters by namespace. No collaborative filtering — Musubi targets a small team / single-operator deployment.
- **Query-drift penalty.** We don't penalize hits on rare terms; sparse handles that organically.
- **Contradiction penalty.** Contradictions do not affect the score. (Excluding flagged contradictions from retrieval with a filter is not currently enforced.)
- **Content-length bonus/malus.** Length is noise here; relevance handles it via dense/sparse scoring.
- **Time-of-day / session context.** Out of scope for v1. A future "contextual recall" layer could use it.

## Why this combination

Generative Agents (Park et al. 2023) combines relevance, recency and importance in a weighted sum. We reproduce that and add two components our planes require:

- **Provenance** is necessary because we have multiple memory types. Without it, a recent provisional episodic memory will out-rank a year-old curated fact on the same topic — undesirable.
- **Reinforcement** matters because concept promotion is our trust-building mechanism. A well-reinforced concept should beat a one-off provisional episodic memory even when both are relevant.

## Tuning

The weights are hand-tuned defaults. The evals harness (see [[05-retrieval/evals]]) runs labelled query corpora; when we change weights, we re-run evals and commit both the weights and the eval report. The ADR template for weight changes is in [[13-decisions/template-weights-change]].

Long-term: we'd like to learn weights from user feedback (thumbs-up/down on retrieval results). Not v1.

## Deterministic tiebreaks

When two hits score identically, `rank_hits` tiebreaks lexicographically on `(object_id, plane)`; the cross-plane merge uses the same key ([[05-retrieval/cross-plane-ranking]]). This keeps retrieval reproducible — essential for test determinism.

## API exposure

Every ranked result surfaces its components. The `ScoreComponents` attribute is `reinforce`; every dict form of the components, `ScoreComponents.as_dict()` and the API response alike, uses the key `reinforcement`. Code that builds a components dict by hand must use `reinforcement`: the cross-plane rescoring step reads that key, and any other spelling is silently scored as 0.

```json
{
  "object_id": "...",
  "score": 0.734,
  "score_components": {
    "relevance": 0.82,
    "recency": 0.72,
    "importance": 0.8,
    "provenance": 1.0,
    "reinforcement": 0.0
  }
}
```

Debugging "why did this result rank here?" is a first-class feature. The evals harness and callers can use these to validate ranking intuitions.

## Test Contract

**Module under test:** `src/musubi/retrieve/scoring.py`

1. `test_score_in_0_1_range_for_any_hit`
2. `test_components_sum_with_weights_equals_total`
3. `test_relevance_normalized_within_batch`
4. `test_relevance_uses_sigmoid_for_rerank_score`
5. `test_relevance_zero_when_batch_max_rrf_is_not_positive`
6. `test_recency_decay_matches_half_life_table`
7. `test_recency_half_life_per_plane_applied`
8. `test_importance_clamped_to_1_10`
9. `test_provenance_values_match_table`
10. `test_provenance_demoted_states_get_0_1`
11. `test_unknown_provenance_defaults_to_0_1`
12. `test_reinforcement_log_scaled`
13. `test_access_count_used_when_reinforcement_count_absent`
14. `test_tiebreak_deterministic_on_object_id`
15. `test_score_components_exposed_on_result`
16. `test_weights_change_shifts_ranking_predictably`
17. `test_no_rng_used_in_scoring` (grep check)

Property:

18. `test_hypothesis_scores_are_monotonic_in_each_component_holding_others_fixed`
19. `test_hypothesis_swapping_weights_reorders_results_consistently_with_the_math`

Eval:

20. The nightly gate requires deep-mode MRR ≥ 0.70 (`src/musubi/evals/gates.py`; see [[05-retrieval/evals]]).
