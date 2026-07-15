---
title: "Slice: RET-004 Quality-Gate Evals"
slice_id: slice-ret004-evals
issue: 430
section: _slices
type: slice
status: in-progress
owner: gemini-3-1
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-13
reviewed: false
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]"]
blocks: []
---

# Slice: RET-004 Quality-Gate Evals

> Defines the versioned corpus, executable quality metrics, CI pipelines, and explicit behavior thresholds required to replace the skipped retrieval testing placeholders.

**Phase:** Retrieval · **Status:** `in-progress` · **Owner:** `gemini-3-1`

## Specs to implement

- [[05-retrieval/evals]]

## Owned paths

- `docs/Musubi/05-retrieval/evals.md`
- `docs/Musubi/_slices/slice-ret004-evals.md`
- `docs/Musubi/_slices/slice-retrieval-hybrid.md`
- `docs/Musubi/_slices/slice-retrieval-scoring.md`
- `src/musubi/evals/`
- `tests/evals/`
- `tests/retrieve/test_scoring.py`
- `tests/retrieve/test_hybrid.py`
- `tests/retrieve/test_rerank.py`
- `tests/retrieve/test_orchestration.py`
- `.github/workflows/evals.yml`

*(Note: The four test files and two parent slice documents listed above intentionally overlap with completed slices / RET007. This is a deliberate transfer of path ownership into this slice for the duration of the evaluation implementation).*

## Acceptance Matrix (Red Contracts)

This slice must implement a tests-first evaluation harness. The existing skipped placeholders (`test_scoring.py:380`, `test_hybrid.py:403`, `test_rerank.py:210`, `test_orchestration.py:50`) must be preserved as historical pointers but replaced entirely by the following explicit, executable gates:

| Gate / Test | Scope & Fault Injection | Acceptance Assertion |
|---|---|---|
| `test_metric_functions_reproduce_known_values` | Hand-feed fixed score lists into MRR/NDCG/Recall metric functions. | Assert exact mathematical output. |
| `test_golden_query_file_schema_validates` | Load malformed/missing fields in corpus YAML/JSONL. | Assert strict Pydantic validation failures. |
| `test_corpus_snapshot_checksum_verified_before_run` | Modify corpus file without bumping manifest hash. | Assert pipeline halt on checksum mismatch. |
| `test_eval_run_deterministic_across_reruns` | Run identical queries 3 times with fixed seed. | Assert identical score ordering and values. |
| `test_holdout_split_excluded_from_tuning_runs` | Queries against partitioned query/label sets over the same corpus. | Assert holdout queries/labels never participate in tuning/calibration. |
| `test_eval_pr_smoke_fixed_embeddings` | PR pipeline: deterministic fake/precomputed embeddings and in-memory store. | Assert logic/weight schema runs without real models (no Qdrant). |
| `test_eval_nightly_qdrant_tei_thresholds` | Scheduled pipeline: Real Qdrant/TEI against 1000-doc BEIR. | Assert mode-specific targets: Fast vs Deep separately for MRR, NDCG@10, Recall@20, P@1. |
| `test_ci_gate_fails_on_ndcg_regression` | Unit test feeding fixed baseline/candidate metrics to the delta logic. | Assert exact pass/fail boundaries. |
| `test_eval_scheduled_baseline_report` | Scheduled integration: runs real `main` vs `candidate` metrics. | Assert pipeline FAILS at explicitly documented degradation deltas. |
| `test_eval_compare_reports_per_query_diffs` | Inject scenario where previously top-ranked hit falls to rank 11. | Assert per-query top-relevant dropping out of top-10 is a FAIL. |
| `test_eval_abstention_fpr` | Explicitly enforce score threshold against pure noise query. | Assert explicitly train-calibrated, versioned score threshold (frozen before holdout). Report FPR and FN/recall tradeoff per mode. |
| `test_eval_contradiction_blending` | Query targeting two matured but contradictory facts. | Assert both facts appear in top-K context. |
| `test_eval_cross_plane_blending` | Query requiring one hit from `curated` and one from `episodic`. | Assert both retrieved accurately via hybrid/RRF. |
| `test_eval_provisional_immediate_recall` | Query targeting a fresh write lacking `matured` provenance. | Include provisional in `state_filter`; assert immediate query ranks target within bounded K while preserving lower authority label. |

## Design Constraints: PR-Smoke Seam Contract
Before implementing the CLI and runner source, the following CLI/workflow seam contract is strictly enforced:
- **Fixture Shape (`smoke_fixture.json`):** Deterministic, network-free, manifest-covered typed extension schema (`musubi.evals.schema.SmokeFixture`) containing both a finite `query_embedding` array and a list of `corpus` documents, each with unique non-empty `id`, exact `text`, integer `relevance` labels, and finite `embedding` vectors equal in dimension to the query vector. Unknown fields fail closed.
- **CLI Seam Verification (`test_eval_cli_seam_fixed_embeddings_red`):** The `musubi.evals smoke` command MUST read the fixture and pass `corpus` and `query_embedding` exactly verbatim into `run_smoke_gate`. The contract strictly fails if the CLI drops `query_embedding`, strips/transforms document fields, or exits early on legacy validations.

## Test Accounting
The `tests/evals/` test suite holds the structural contracts for the bootstrap and seam:
- `tests/evals/test_evals_contract.py`: Holds the discrimination tests for `run_smoke_gate` behavior.
- `tests/evals/test_cli.py`: Holds the PR-Smoke Seam CLI behavior and `SmokeFixture` structural validation strict reds (29 tests: 6 passes, 23 strict xfails).
- `tests/evals/test_ci_bootstrap_contract.py`: Holds the `.github/workflows/evals.yml` exact structural bootstrap validations.

## Work log

### 2026-07-14 — claude-code-opus48 (aoi): merge-to-main + Closure-Rule repair (PR #438)

Merged current `main` into the branch (merge commit, no force-push) and closed the Test-Contract
Closure gap under frozen RET-004 scope (no new metrics/corpus/redesign):

- **Six spec-harness bullets** were implemented under deviating names; renamed the passing tests to the
  spec-verbatim names (no duplicated matrices): `…corpus_schema_validation→golden_query_file_schema_validates`,
  `…corpus_manifest_checksum→corpus_snapshot_checksum_verified_before_run`,
  `…deterministic_rerun→eval_run_deterministic_across_reruns`,
  `…per_query_top_hit_drop→eval_compare_reports_per_query_diffs`,
  `…baseline_delta_gate_unit→ci_gate_fails_on_ndcg_regression`,
  `…holdout_isolation→holdout_split_excluded_from_tuning_runs`. The Test-Accounting table above is updated to match.
- **Bullet 2 (`test_metric_functions_reproduce_known_values`, "NDCG/MRR/Recall formulas") was genuinely
  partial** — `metrics.py` computed NDCG only. Added the smallest real correction: standard `rr()`
  (reciprocal rank → MRR term) and `recall_at_k()` in `src/musubi/evals/metrics.py`, and extended the
  renamed test to assert known values for all three. No new wrong-candidate matrix.
- **Integration bullets 8–11** (`@pytest.mark.evals`, synthetic BEIR-mini + household corpus) are
  out-of-scope for the unit CI gate and run in the separate scheduled/evals CI stage — deferred there,
  not to a follow-up slice.

Gates green: `make check` (ruff format+check, mypy strict, pytest, coverage ≥85), focused eval + PR smoke,
`make tc-coverage SLICE=slice-ret004-evals` → Closure Rule satisfied (7/7 harness bullets passing).
