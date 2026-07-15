---
owner: gemini-3-1-shiori
status: in-review
issue: 534
title: "Slice: ING-001 semantic dedup strict compatibility bounds"
slice_id: slice-issue534-ing001-semantic-dedup
section: _slices
type: slice
phase: "Ingestion"
tags:
  - section/slices
  - status/in-review
  - type/slice
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---
# Slice: ING-001 semantic dedup strict compatibility bounds

## Context
Semantic dedup may merge only factually compatible duplicates. Corrections, negations, participant or time changes, conflicting numbers, and ambiguous near-matches must remain distinct and preserve lineage.

## Specs to implement
- [[06-ingestion/capture]] (assuming deduplication rule is documented here)

## Owned paths
- `src/musubi/planes/episodic/plane.py`
- `tests/planes/test_episodic.py`

## Forbidden paths
- Qdrant cluster indexing logic, LLM adapter internal implementations. No new engine.

## Test Contract
- `test_semantic_dedup_merges_exact_duplicate`
- `test_semantic_dedup_merges_normalized_duplicate`
- `test_semantic_dedup_rejects_correction`
- `test_semantic_dedup_rejects_negation`
- `test_semantic_dedup_rejects_participant_change`
- `test_semantic_dedup_rejects_time_change`
- `test_semantic_dedup_rejects_conflicting_numbers`
- `test_semantic_dedup_rejects_ambiguity`
- `test_semantic_dedup_rejects_language_token_punctuation`
- `test_semantic_dedup_compares_content_not_summary`
- `test_semantic_dedup_rejects_paraphrase`
- `test_semantic_dedup_rejects_participants_change`

## Definition of Done
- Strict factual compatibility evaluated during dedup.
- `make check` is fully passing.

## Work log

- 2026-07-15 — Added a fail-closed factual-compatibility gate to single and
  batch episodic dedup. The labeled corpus in
  `tests/planes/test_episodic.py` covers every Test Contract bullet, including
  normalization-equivalent duplicates, corrections, negations, paraphrases,
  content and structured-participant changes, time and numeric conflicts,
  ambiguity, punctuation, and content-versus-summary behavior. Focused dedup
  tests and exact-head CI passed before handoff.
