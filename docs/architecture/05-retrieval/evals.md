---
title: Retrieval Evals
section: 05-retrieval
tags: [benchmarks, evals, quality, retrieval, section/retrieval, status/research-needed, type/spec]
type: spec
status: research-needed
updated: 2026-04-17
up: "[[05-retrieval/index]]"
reviewed: false
---
# Retrieval Evals

How we measure retrieval quality. Without evals, tuning is vibes; with them, every weight change and model swap is defensible.

## Layers

Three layers of evaluation, increasing in cost:

1. **Unit + property tests** — deterministic, fast, run on every commit.
2. **Golden set replay** — hand-curated query/answer pairs with ground-truth object IDs, run nightly and pre-release.
3. **Live shadow evals** — periodically sample real queries, shadow-run with an alt config, measure NDCG@10 delta offline.

## Golden sets

### Structure

```
musubi/evals/golden/
├── README.md
├── corpora/
│   ├── household-2026-04/
│   │   ├── manifest.json             # source snapshots (git SHA, data hash)
│   │   └── qdrant-backup/            # snapshot of collections (volume backup)
│   └── synthetic-beir-mini/
│       └── ...
└── queries/
    ├── household-2026-04.yaml
    └── synthetic-beir-mini.yaml
```

### Query file format

```yaml
# queries/household-2026-04.yaml
corpus: household-2026-04
queries:
  - id: q001
    text: "how do I restart the livekit agent"
    relevant:
      - object_id: 2W1eA1aaaaaaaaaaaaaaaa
        relevance: 3            # 0-3 graded; 3 = perfect, 2 = good, 1 = partial, 0 = not relevant
      - object_id: 2W1eB2bbbbbbbbbbbbbbbb
        relevance: 2
    mode: fast
    namespace: eric/claude-code/blended
    # expected budget
    latency_p95_ms: 400

  - id: q002
    text: "what did we decide about promoting concepts to curated knowledge"
    relevant:
      - object_id: 2W1eC3cccccccccccccccc
        relevance: 3
      - object_id: 2W1eD4dddddddddddddddd
        relevance: 2
    mode: deep
    namespace: eric/_shared/blended
    latency_p95_ms: 3000
```

~200 queries is a healthy household-sized golden set. We seed with 50 hand-written queries + 150 LLM-expanded ones (a local LLM generates paraphrases from the 50 originals; a human reviews and keeps the good ones).

### Graded relevance (0-3)

We use graded rather than binary relevance because NDCG is our primary metric and gradations matter. Guidelines:

- **3 — Directly answers the query.**
- **2 — Relevant and helpful, but secondary to the main answer.**
- **1 — Tangentially related; useful context but not an answer.**
- **0 — Not relevant.**

Missing a `3` at rank 1 is worse than missing a `1` at rank 10; NDCG captures this.

## Metrics

Computed per query, averaged across the set:

| Metric | Meaning | Target (default weights) |
|---|---|---|
| **NDCG@10** | Rank-sensitive quality of top-10 | Fast ≥ 0.55; Deep ≥ 0.65 |
| **MRR** | 1/rank of first relevant | Fast ≥ 0.55; Deep ≥ 0.70 |
| **Recall@20** | Fraction of relevant hits in top-20 | Fast ≥ 0.70; Deep ≥ 0.85 |
| **P@1** | Is the first result perfect (relevance=3)? | Fast ≥ 0.40; Deep ≥ 0.55 |
| **Latency p50 / p95** | End-to-end retrieve time | per-query `latency_p95_ms` |

Thresholds are hand-tuned on initial seed queries — they'll shift as the golden set grows. Every weight / model change commits an eval report before merging.

## Tooling

```bash
musubi-cli eval run --corpus household-2026-04 --mode fast
musubi-cli eval run --corpus household-2026-04 --mode deep
musubi-cli eval compare --before main --after pr-123
```

`eval compare` diffs two runs and reports:

- Per-metric delta (NDCG@10 ±, MRR ±, …)
- Queries where rank of top-relevant changed by ≥ 3 positions
- Queries where a relevant hit dropped out of top-10 (regression)

All golden runs are reproducible: same corpus snapshot + same model versions + same weights + same seed = same metrics. Non-reproducible runs are a bug.

## Corpus snapshots

Each corpus directory has a `manifest.json`:

```json
{
  "name": "household-2026-04",
  "created_at": "2026-04-17T00:00:00Z",
  "qdrant_snapshot_sha256": "...",
  "model_versions": {
    "dense": "BAAI/bge-m3@v1.0",
    "sparse": "naver/splade-v3@v1.0",
    "reranker": "BAAI/bge-reranker-v2-m3@v1.0"
  },
  "point_counts": {
    "musubi_episodic": 8432,
    "musubi_curated": 412,
    "musubi_concept": 88,
    "musubi_artifact_chunks": 21035
  },
  "schema_version": 1
}
```

When we re-embed (model swap), the corpus snapshot is re-created — we don't mutate an existing snapshot.

## Regression gates

CI gates on eval delta:

- **NDCG@10 drop > 2 points** → CI fails, commit blocked.
- **MRR drop > 3 points** → CI fails.
- **Latency p95 regression > 20%** → CI fails.
- **Any golden query drops its top-relevant hit out of top-10** → CI fails with the list.

Overrides require an ADR documenting why. No silent regressions.

## Live shadow eval

Periodically (weekly), a sample of real queries from production is replayed offline against an alt config (e.g., new weights, new model). Metrics are computed offline; alt config is promoted only if metrics are non-negative.

Privacy: only query-text and result IDs are stored, never full response content. All shadow data is purgeable on request.

## Ragas-style metrics (future)

Future additions from Ragas ([https://arxiv.org/abs/2309.15217](https://arxiv.org/abs/2309.15217)):

- **Context precision** — what fraction of retrieved chunks ended up being cited by the downstream LLM?
- **Context recall** — of the chunks that should have been cited, what fraction were in the top-k?
- **Faithfulness** (on LLM responses) — does the generation adhere to the retrieved context?

These require ground-truth citations from LLM traces. We'll add them once LLM-in-the-loop is part of the system. Not v1.

## Anti-gaming

Because retrieval weights affect eval metrics, a common trap is overfitting: tune weights until the golden set shines, but real queries regress.

Mitigations:

- **Holdout split**: 20% of golden queries are never used for tuning — only for validation. If the holdout NDCG@10 drops, that's a real regression.
- **Cross-corpus evals**: we keep the BEIR-mini synthetic corpus as a second benchmark. Improvements should generalize.
- **Shadow eval**: as above — real traffic sampling is the strongest anti-overfitting signal.

## Running evals as tests

Golden-set replay is a pytest fixture:

```python
@pytest.mark.evals
def test_household_golden_fast_ndcg():
    results = run_evals(corpus="household-2026-04", mode="fast")
    assert results.metrics.ndcg_at_10 >= 0.55
    assert results.metrics.mrr >= 0.55

@pytest.mark.evals
def test_household_golden_deep_ndcg():
    results = run_evals(corpus="household-2026-04", mode="deep")
    assert results.metrics.ndcg_at_10 >= 0.65
```

Marked `evals` so they can be excluded from the fast unit test suite and run in a separate CI stage with the corpus snapshot mounted.

## Test contract (meta)

**Module under test:** `musubi/evals/`

Harness:

1. `test_golden_query_file_schema_validates`
2. `test_metric_functions_reproduce_known_values` (unit tests on NDCG/MRR/Recall formulas)
3. `test_corpus_snapshot_checksum_verified_before_run`
4. `test_eval_run_deterministic_across_reruns`
5. `test_eval_compare_reports_per_query_diffs`
6. `test_ci_gate_fails_on_ndcg_regression`
7. `test_holdout_split_excluded_from_tuning_runs`

Integration (slow, gated by `@pytest.mark.evals`):

8. `integration: synthetic BEIR-mini fast NDCG@10 ≥ 0.50`
9. `integration: synthetic BEIR-mini deep NDCG@10 ≥ 0.60`
10. `integration: household corpus fast + deep meet all threshold metrics`
11. `integration: repeat run produces identical metrics` (reproducibility)
