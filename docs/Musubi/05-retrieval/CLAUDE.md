---
title: "Agent Rules — Retrieval (05)"
section: 05-retrieval
type: index
status: complete
tags: [section/retrieval, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: true
---

# Agent Rules — Retrieval (05)

Local rules for changes under `src/musubi/retrieve/` (reranking lives in `src/musubi/retrieve/rerank.py`; the TEI clients, including the reranker client, live in `src/musubi/embedding/tei.py`). Supplements [[CLAUDE]].

## Must

- **Weighted score only.** The single scoring function in [[05-retrieval/scoring-model]] is the source of truth. Don't invent per-path variants — parameterise the one.
- **Hybrid search is the default.** Use the Qdrant Query API with server-side RRF fusion. Never run dense + sparse in two round-trips.
- **Budget-aware paths.** Fast path targets **< 400 ms p95 with colocated inference** (TEI and Qdrant on the same host as core); its deadlines are the settings `retrieval_fast_encoding_timeout_s`, `retrieval_fast_plane_timeout_s` and `retrieval_fast_whole_timeout_s` (defaults 0.25 / 0.25 / 0.4 s, `src/musubi/settings.py`). Deep path targets < 5 s p95 and is bounded by a 5 s whole-call deadline in `src/musubi/retrieve/orchestration.py`. If a change pushes latency past budget, it must be gated behind a feature flag or reverted.
- **Filters in Qdrant, never in Python.** Every filter you'd write as a list comprehension can live in the query as `must` / `must_not` / `should`.
- **Every hit carries provenance.** `plane`, `object_id`, `namespace`, `score_components`, `lineage`. The caller must be able to answer "why did this rank this way?".

## Must not

- Loop `set_payload`. Use `batch_update_points` with `SetPayloadOperation`. This is a common bug.
- Call the reranker on anything larger than the top-N from hybrid. Budget is your constraint.
- Change scoring weights without an ADR. See [[13-decisions/template-weights-change]] for the template.

## Latency budgets

These are targets, not CI gates: there is no `tests/perf/` suite. The deadlines that are enforced in code are timeouts, not percentile checks.

| Path               | Target              | Enforced in code |
|--------------------|---------------------|------------------|
| Fast path          | 400 ms p95 (colocated inference) | whole-call `retrieval_fast_whole_timeout_s` (0.4 s), encoding and per-plane deadlines 0.25 s each |
| Deep path          | 5 s p95             | 5 s whole-call `asyncio.wait_for` per target in `orchestration.py` |
| Reranker stage     | see [[05-retrieval/reranker]] | `retrieval_rerank_timeout_s` (1.5 s); on expiry deep returns hybrid order with a `reranker_failed` warning |
| Hybrid query (deep, per plane) | — | 1.5 s per plane, sparse encoding 1.0 s then dense-only (`src/musubi/retrieve/deep.py`) |

## Adding a new score component

1. Write the ADR using [[13-decisions/template-weights-change]].
2. Add the field to `ScoredHit`.
3. Default weight to 0 (off) behind a flag.
4. Tune weight behind a shadow-eval (see [[05-retrieval/evals]]).
5. Graduate via ADR.
