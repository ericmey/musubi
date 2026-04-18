---
title: Scoring Model
section: 05-retrieval
tags: [ranking, retrieval, scoring, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
---
# Scoring Model

The single function that turns a raw retrieval hit into a rank-orderable number. Used by fast path, deep path, and the blended cross-plane merger.

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

    total = (
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

Default weights (tunable in `config.py`):

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

Taken directly from Qdrant's server-side RRF fusion score. RRF produces scores in roughly the range [0, 1/k + 1/k + ...] with k=60 by default. We normalize within a result batch by `max_rrf` to land in [0, 1]:

```python
def _relevance(hit: Hit) -> float:
    return hit.rrf_score / hit.batch_max_rrf
```

If only one search mode is used (dense-only or sparse-only; rare), we fall back to the raw similarity score normalized to [0, 1] by the same technique.

### Recency (0..1)

Exponential decay over age, in hours:

```python
def _recency(hit: Hit, now: float) -> float:
    age_hours = max(0.0, (now - hit.updated_epoch) / 3600)
    # half-life: 30 days
    return math.exp(-age_hours * math.log(2) / (30 * 24))
```

A 30-day half-life means:

- Same-day: ~1.0
- 7 days old: ~0.85
- 30 days old: 0.5
- 90 days old: 0.125
- 1 year old: ~0.0002

Half-life is per-plane tunable (`RECENCY_HALF_LIFE_EPISODIC=30d`, `RECENCY_HALF_LIFE_CURATED=180d` — curated facts age slower, since they're generally more durable).

### Importance (0..1)

```python
def _importance(hit: Hit) -> float:
    return max(1, min(10, hit.importance)) / 10.0
```

Importance is an object-level field (1-10), set at capture time by the LLM scorer in maturation (for episodic) or by the human (for curated). Defaults: episodic=5, curated=7, concept=6.

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
| artifact chunk | matured | 0.7 |

Any object in `demoted`, `archived`, or `superseded` states defaults to 0.1 (they're rarely returned in the first place, but if filters let them through, they're demoted in score).

### Reinforcement (0..1)

Log-scaled count:

```python
def _reinforcement(hit: Hit) -> float:
    return min(1.0, math.log1p(hit.reinforcement_count) / math.log1p(20))
```

- 0 reinforcements → 0.0
- 1 → 0.23
- 3 → 0.45
- 10 → 0.79
- 20+ → 1.0

For non-concept types that don't track `reinforcement_count`, we substitute `access_count`, log-scaled similarly but with a larger cap (100) — reflects that re-access is weaker evidence than re-synthesis.

## What isn't in the score (and why)

- **Per-user affinity / personalization.** Everything already filters by namespace. No collaborative filtering — single-household.
- **Query-drift penalty.** We don't penalize hits on rare terms; sparse handles that organically.
- **Contradiction penalty.** If a concept is flagged `contradicts`, it's excluded from retrieval via a filter, not via score. Binary.
- **Content-length bonus/malus.** Length is noise here; relevance handles it via dense/sparse scoring.
- **Time-of-day / session context.** Out of scope for v1. A future "contextual recall" layer could use it.

## Why this combination

From Generative Agents (Park et al. 2023): relevance × recency × importance with a learned weighted sum improves recall over relevance-only retrieval by ~15 pp on human-rated tasks. We reproduce that and add two components our planes require:

- **Provenance** is necessary because we have multiple memory types. Without it, a recent provisional episodic memory will out-rank a year-old curated fact on the same topic — undesirable.
- **Reinforcement** matters because concept promotion is our trust-building mechanism. A well-reinforced concept should beat a one-off provisional episodic memory even when both are relevant.

## Tuning

The weights are hand-tunable defaults. We have an evals harness (see [[05-retrieval/evals]]) with a golden query set; when we change weights, we re-run evals and commit both the weights and the eval report. The ADR template for weight changes is in [[13-decisions/template-weights-change]].

Long-term: we'd like to learn weights from user feedback (thumbs-up/down on retrieval results). Not v1.

## Deterministic tiebreaks

When two hits score identically (floats collide ~once in 10^9), we tiebreak lexicographically on `(object_id, plane)`. This keeps retrieval reproducible — essential for test determinism.

## API exposure

Every result surfaces its components:

```json
{
  "object_id": "...",
  "score": 0.734,
  "score_components": {
    "relevance": 0.82,
    "recency": 0.72,
    "importance": 0.8,
    "provenance": 1.0,
    "reinforce": 0.0
  }
}
```

Debugging "why did this result rank here?" is a first-class feature. The Slow Thinker and the evals harness both use these to validate ranking intuitions.

## Test contract

**Module under test:** `musubi/retrieval/scoring.py`

1. `test_score_in_0_1_range_for_any_hit`
2. `test_components_sum_with_weights_equals_total`
3. `test_relevance_normalized_within_batch`
4. `test_recency_decay_matches_half_life_table`
5. `test_recency_half_life_per_plane_applied`
6. `test_importance_clamped_to_1_10`
7. `test_provenance_values_match_table`
8. `test_provenance_demoted_states_get_0_1`
9. `test_reinforcement_log_scaled`
10. `test_tiebreak_deterministic_on_object_id`
11. `test_score_components_exposed_on_result`
12. `test_weights_change_shifts_ranking_predictably`
13. `test_no_rng_used_in_scoring` (grep check)

Property:

14. `hypothesis: scores are monotonic in each component holding others fixed`
15. `hypothesis: swapping weights reorders results consistently with the math`

Eval:

16. `eval: golden query set MRR ≥ 0.7 with default weights` (see [[05-retrieval/evals]])
