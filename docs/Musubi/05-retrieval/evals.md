---
title: Retrieval Evals
section: 05-retrieval
tags: [benchmarks, evals, quality, retrieval, section/retrieval, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[05-retrieval/index]]"
reviewed: false
implements: ["src/musubi/evals/", "tests/evals/"]
---
# Retrieval Evals

How we measure retrieval quality. Without evals, tuning is vibes; with them, every weight change and model swap is defensible.

## What exists

- **Code:** `src/musubi/evals/` — `cli.py` (entry point), `metrics.py` (NDCG@k, reciprocal rank, Recall@k), `gates.py` (thresholds and regression checks), `live_gate.py` and `scheduled_gate.py` (the live gate), `runner.py` (smoke gate), `schema.py` and `corpus.py` (fixture schema and manifest checksums).
- **Data:** `tests/evals/data/` — `corpus.yaml` (query-file schema sample), `scheduled_corpus.yaml` (the graded corpus the live gate seeds), `smoke_fixture.json` (fixed embeddings for the PR smoke gate), `baseline.json` (smoke baseline) and `manifest.json` (SHA-256 of each data file).
- **CI:** `.github/workflows/evals.yml` — a smoke gate on every pull request to `main`, and a nightly scheduled gate (cron `0 0 * * *`, also runnable on demand) against a real Qdrant + TEI stack.

## Layers

1. **Unit + property tests** — deterministic, fast, run on every commit (`tests/retrieve/`, `tests/evals/`).
2. **PR smoke gate** — `python -m musubi.evals smoke`: a deterministic, network-free run over fixed embeddings. Fails below NDCG@10 0.5, or on a drop of more than 0.02 from `baseline.json`.
3. **Scheduled live gate** — `python -m musubi.evals scheduled`: boots Qdrant + TEI (`deploy/test-env/docker-compose.test.yml`), seeds the checksum-pinned `scheduled_corpus.yaml` into a fresh run-scoped namespace through the production write path, runs every query through real retrieval, enforces the per-mode thresholds below, and tears the run data down. Without TEI it fails loud (exit 3) rather than inventing numbers. The same nightly job also runs the BEIR-style hybrid-vs-dense measurement (`test_integration_beir_style_eval_on_1000_doc_synthetic_corpus_hybrid_beats_dense_only_by_2_ndcg10_points`). A failed nightly run opens or updates a tracking issue; a green run closes it.

Live shadow evals (replaying sampled real queries against an alternative config) are **planned, not implemented**.

## Corpora

### Query file format

The golden query schema is `GoldenQuery` in `src/musubi/evals/schema.py`:

```yaml
id: q001
text: "how do I restart the voice agent"
relevant:
  - object_id: "2W1eA1aaaaaaaaaaaaaaaa"
    relevance: 3            # 0-3 graded; 3 = perfect, 2 = good, 1 = partial, 0 = not relevant
  - object_id: "2W1eB2bbbbbbbbbbbbbbbb"
    relevance: 2
mode: fast
namespace: alex/claude-code/episodic
```

The scheduled corpus (`tests/evals/data/scheduled_corpus.yaml`) lists the documents to seed and the graded queries against them. It deliberately includes hard distractors that echo a query's surface words while meaning something else, so a naive lexical ranker fails the thresholds (`tests/evals/test_scheduled_corpus_contract.py`).

### Graded relevance (0-3)

We use graded rather than binary relevance because NDCG is our primary metric and gradations matter. Guidelines:

- **3 — Directly answers the query.**
- **2 — Relevant and helpful, but secondary to the main answer.**
- **1 — Tangentially related; useful context but not an answer.**
- **0 — Not relevant.**

Missing a `3` at rank 1 is worse than missing a `1` at rank 10; NDCG captures this.

## Metrics

Computed per query, averaged per mode (`src/musubi/evals/live_gate.py`):

| Metric | Meaning | Nightly threshold (`gates.py`) |
|---|---|---|
| **NDCG@10** | Rank-sensitive quality of top-10 | Fast ≥ 0.55; Deep ≥ 0.65 |
| **MRR** | 1/rank of first relevant | Fast ≥ 0.55; Deep ≥ 0.70 |
| **Recall@20** | Fraction of relevant hits in top-20 | Fast ≥ 0.70; Deep ≥ 0.85 |
| **P@1** | Is the first result perfect (relevance=3)? | Fast ≥ 0.40; Deep ≥ 0.55 |

Thresholds are frozen: a run below them reports the raw per-query and per-mode results and fails, and the thresholds are never tuned to make a run green. Every weight or model change should commit an eval report before merging.

## Tooling

```bash
uv run python -m musubi.evals smoke --data-dir tests/evals/data
uv run python -m musubi.evals scheduled --data-dir tests/evals/data   # needs Qdrant + TEI
```

Those are the only two commands. Comparison helpers exist as library functions in `src/musubi/evals/gates.py`, not as CLI commands:

- `check_delta_tolerances` — fails on an NDCG@10 drop of more than 0.02, an MRR drop of more than 0.03, or a p95 latency regression of more than 20%.
- `check_top_hit_drops` — fails when a query's top hit drops out of the candidate's top-10.
- `check_abstention_fpr` — false-positive / false-negative rates against per-mode score thresholds, for queries that should return nothing.

Runs are meant to be reproducible: same corpus + same model versions + same weights = same metrics. Non-reproducible runs are a bug.

## Corpus integrity

`tests/evals/data/manifest.json` maps each data file to its SHA-256. The CLI verifies the manifest before every run and refuses files outside the data directory. When the corpus changes, the manifest is regenerated in the same commit.

## Regression gates

- **PR smoke:** NDCG@10 below 0.5, or more than 0.02 below `baseline.json`, fails the PR check.
- **Nightly:** any per-mode metric below its threshold fails the scheduled job; so does the BEIR hybrid-vs-dense step.

Overrides require an ADR documenting why. No silent regressions.

## Ragas-style metrics (future)

Future additions from Ragas ([https://arxiv.org/abs/2309.15217](https://arxiv.org/abs/2309.15217)):

- **Context precision** — what fraction of retrieved chunks ended up being cited by the downstream LLM?
- **Context recall** — of the chunks that should have been cited, what fraction were in the top-k?
- **Faithfulness** (on LLM responses) — does the generation adhere to the retrieved context?

These require ground-truth citations from LLM traces. We'll add them once LLM-in-the-loop is part of the system. Not v1.

## Anti-gaming

Because retrieval weights affect eval metrics, a common trap is overfitting: tune weights until the corpus shines, but real queries regress.

Mitigations:

- **Holdout split**: `run_isolated_eval` (`src/musubi/evals/runner.py`) keeps holdout queries out of tuning runs.
- **Cross-corpus evals**: the BEIR-style synthetic corpus is a second benchmark. Improvements should generalize.
- **Hard distractors**: the scheduled corpus is built so a surface-token ranker fails.

## Test Contract

**Module under test:** `src/musubi/evals/`

Harness:

1. `test_golden_query_file_schema_validates`
2. `test_metric_functions_reproduce_known_values` (unit tests on NDCG/MRR/Recall formulas)
3. `test_corpus_snapshot_checksum_verified_before_run`
4. `test_eval_run_deterministic_across_reruns`
5. `test_eval_compare_reports_per_query_diffs`
6. `test_ci_gate_fails_on_ndcg_regression`
7. `test_holdout_split_excluded_from_tuning_runs`
8. `test_eval_nightly_qdrant_tei_thresholds`
9. `test_eval_abstention_fpr`

Live gate and CLI:

10. `test_run_live_gate_groups_by_mode_and_enforce_catches_subthreshold`
11. `test_scheduled_command_fails_loud_without_tei`
12. `test_naive_lexical_ranker_does_not_clear_the_frozen_thresholds`
13. `test_scheduled_workflow_live_gate_contract`
