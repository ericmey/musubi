---
title: "Slice: RET-004 Quality-Gate Evals"
slice_id: slice-ret004-evals
section: _slices
type: slice
status: ready
owner: tbd
phase: "Retrieval"
tags: [section/slices, status/ready, type/slice]
updated: 2026-07-13
reviewed: false
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]"]
blocks: []
---

# Slice: RET-004 Quality-Gate Evals

> Defines the versioned corpus, executable quality metrics, CI pipelines, and explicit behavior thresholds required to replace the skipped retrieval testing placeholders.

**Phase:** Retrieval · **Status:** `ready` · **Owner:** `tbd`

## Specs to implement

- [[05-retrieval/evals]]

## Owned paths

- `docs/Musubi/05-retrieval/evals.md`
- `src/musubi/evals/`
- `tests/evals/`
- `.github/workflows/evals.yml`

## Acceptance Matrix (Red Contracts)

This slice must implement a tests-first evaluation harness. The existing skipped placeholders (`test_scoring.py:380`, `test_hybrid.py:403`, `test_rerank.py:210`, `test_orchestration.py:50`) must be preserved as historical pointers but replaced entirely by the following explicit, executable gates:

| Gate / Test | Scope & Fault Injection | Acceptance Assertion |
|---|---|---|
| `test_eval_metric_formula_correctness` | Hand-feed fixed score lists into MRR/NDCG/Recall metric functions. | Assert exact mathematical output. |
| `test_eval_corpus_schema_validation` | Load malformed/missing fields in corpus YAML/JSONL. | Assert strict Pydantic validation failures. |
| `test_eval_corpus_manifest_checksum` | Modify corpus file without bumping manifest hash. | Assert pipeline halt on checksum mismatch. |
| `test_eval_deterministic_rerun` | Run identical queries 3 times with fixed seed. | Assert identical score ordering and values. |
| `test_eval_holdout_isolation` | Run queries against partitioned test vs train sets. | Assert test queries never see train documents. |
| `test_eval_pr_smoke_fixed_embeddings` | PR pipeline: mock embedder/Qdrant, use pre-computed vectors. | Assert logic/weight schema runs without real models. |
| `test_eval_nightly_qdrant_tei_thresholds` | Scheduled pipeline: Real Qdrant/TEI against 1000-doc BEIR. | Assert MRR >= 0.70, NDCG@10 >= 0.65. |
| `test_eval_baseline_delta_enforcement` | Compare `main` metrics to `pr` metrics; inject -0.05 regression. | Assert PR fails pipeline on regression. |
| `test_eval_per_query_top_hit_drop` | Inject scenario where previously top-ranked hit falls to rank 11. | Assert warning generated for per-query regression. |
| `test_eval_abstention_fpr` | Explicitly enforce score threshold (e.g. 0.3) against pure noise query. | Assert `hits == 0` (no hallucinated fallback matches). |
| `test_eval_contradiction_blending` | Query targeting two matured but contradictory facts. | Assert both facts appear in top-K context. |
| `test_eval_cross_plane_blending` | Query requiring one hit from `curated` and one from `episodic`. | Assert both retrieved accurately via hybrid/RRF. |
| `test_eval_provisional_immediate_recall` | Query targeting a fresh write lacking `matured` provenance. | Assert memory retrieved without punitive score thresholding. |
