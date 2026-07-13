---
title: "Slice: RET-004 Quality-Gate Evals"
slice_id: slice-ret004-evals
issue: 430
section: _slices
type: slice
status: ready
owner: shiori
phase: "Retrieval"
tags: [section/slices, status/ready, type/slice]
updated: 2026-07-13
reviewed: false
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]"]
blocks: []
---

# Slice: RET-004 Quality-Gate Evals

> Defines the versioned corpus, executable quality metrics, CI pipelines, and explicit behavior thresholds required to replace the skipped retrieval testing placeholders.

**Phase:** Retrieval · **Status:** `ready` · **Owner:** `shiori`

## Specs to implement

- [[05-retrieval/evals]]

## Owned paths

- `docs/Musubi/05-retrieval/evals.md`
- `docs/Musubi/_slices/slice-ret004-evals.md`
- `src/musubi/evals/`
- `tests/evals/`
- `tests/retrieve/test_scoring.py`
- `tests/retrieve/test_hybrid.py`
- `tests/retrieve/test_rerank.py`
- `tests/retrieve/test_orchestration.py`
- `.github/workflows/evals.yml`

## Acceptance Matrix (Red Contracts)

This slice must implement a tests-first evaluation harness. The existing skipped placeholders (`test_scoring.py:380`, `test_hybrid.py:403`, `test_rerank.py:210`, `test_orchestration.py:50`) must be preserved as historical pointers but replaced entirely by the following explicit, executable gates:

| Gate / Test | Scope & Fault Injection | Acceptance Assertion |
|---|---|---|
| `test_eval_metric_formula_correctness` | Hand-feed fixed score lists into MRR/NDCG/Recall metric functions. | Assert exact mathematical output. |
| `test_eval_corpus_schema_validation` | Load malformed/missing fields in corpus YAML/JSONL. | Assert strict Pydantic validation failures. |
| `test_eval_corpus_manifest_checksum` | Modify corpus file without bumping manifest hash. | Assert pipeline halt on checksum mismatch. |
| `test_eval_deterministic_rerun` | Run identical queries 3 times with fixed seed. | Assert identical score ordering and values. |
| `test_eval_holdout_isolation` | Queries against partitioned query/label sets over the same corpus. | Assert holdout queries/labels never participate in tuning/calibration. |
| `test_eval_pr_smoke_fixed_embeddings` | PR pipeline: deterministic fake/precomputed embeddings and in-memory store. | Assert logic/weight schema runs without real models (no Qdrant). |
| `test_eval_nightly_qdrant_tei_thresholds` | Scheduled pipeline: Real Qdrant/TEI against 1000-doc BEIR. | Assert mode-specific targets: Fast vs Deep separately for MRR, NDCG@10, Recall@20, P@1. |
| `test_eval_baseline_delta_gate_unit` | Unit test feeding fixed baseline/candidate metrics to the delta logic. | Assert exact pass/fail boundaries. |
| `test_eval_scheduled_baseline_report` | Scheduled integration: runs real `main` vs `candidate` metrics. | Assert pipeline FAILS at explicitly documented degradation deltas. |
| `test_eval_per_query_top_hit_drop` | Inject scenario where previously top-ranked hit falls to rank 11. | Assert per-query top-relevant dropping out of top-10 is a FAIL. |
| `test_eval_abstention_fpr` | Explicitly enforce score threshold against pure noise query. | Assert explicitly train-calibrated, versioned score threshold (frozen before holdout). Report FPR and FN/recall tradeoff per mode. |
| `test_eval_contradiction_blending` | Query targeting two matured but contradictory facts. | Assert both facts appear in top-K context. |
| `test_eval_cross_plane_blending` | Query requiring one hit from `curated` and one from `episodic`. | Assert both retrieved accurately via hybrid/RRF. |
| `test_eval_provisional_immediate_recall` | Query targeting a fresh write lacking `matured` provenance. | Include provisional in `state_filter`; assert immediate query ranks target within bounded K while preserving lower authority label. |
