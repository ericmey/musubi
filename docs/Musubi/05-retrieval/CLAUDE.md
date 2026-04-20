---
title: "Agent Rules — Retrieval (05)"
section: 05-retrieval
type: index
status: complete
tags: [section/retrieval, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: true
---

# Agent Rules — Retrieval (05)

Local rules for slices under `musubi/retrieve/` and `musubi/rerank/`. Supplements [[CLAUDE]].

## Must

- **Weighted score only.** The single scoring function in [[05-retrieval/scoring-model]] is the source of truth. Don't invent per-path variants — parameterise the one.
- **Hybrid search is the default.** Use the Qdrant Query API with server-side RRF fusion. Never run dense + sparse in two round-trips.
- **Budget-aware paths.** Fast path has a hard **< 400 ms p95** budget; deep path < 5 s p95. If a change pushes latency past budget, it must be gated behind a feature flag or reverted.
- **Filters in Qdrant, never in Python.** Every filter you'd write as a list comprehension can live in the query as `must` / `must_not` / `should`.
- **Every hit carries provenance.** `plane`, `object_id`, `namespace`, `score_components`, `lineage`. The caller must be able to answer "why did this rank this way?".

## Must not

- Loop `set_payload`. Use `batch_update_points` with `SetPayloadOperation`. This is the single most common POC bug.
- Call the reranker on anything larger than the top-N from hybrid. Budget is your constraint.
- Change scoring weights without an ADR. See [[13-decisions/template-weights-change]] for the template.

## Latency budgets (enforced in CI)

| Path               | p95 budget | Smoke test                                     |
|--------------------|------------|------------------------------------------------|
| Fast path          | 400 ms     | `tests/perf/test_fast_path_budget.py`          |
| Deep path          | 5 s        | `tests/perf/test_deep_path_budget.py`          |
| Reranker call      | 250 ms / 100 pairs | `tests/perf/test_reranker_budget.py`   |
| Hybrid query       | 150 ms     | `tests/perf/test_hybrid_budget.py`             |

## Adding a new score component

1. Write the ADR using [[13-decisions/template-weights-change]].
2. Add the field to `ScoredHit`.
3. Default weight to 0 (off) behind a flag.
4. Tune weight behind a shadow-eval (see [[05-retrieval/evals]]).
5. Graduate via ADR.

## Related slices

- [[_slices/slice-retrieval-scoring]], [[_slices/slice-retrieval-hybrid]], [[_slices/slice-retrieval-fast]], [[_slices/slice-retrieval-deep]], [[_slices/slice-retrieval-rerank]], [[_slices/slice-retrieval-blended]], [[_slices/slice-retrieval-orchestration]].
