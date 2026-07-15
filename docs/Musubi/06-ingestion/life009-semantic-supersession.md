---
title: LIFE-009 Semantic Supersession with Abstention
section: 06-ingestion
type: contract
status: active
tags: [section/ingestion, status/active, type/contract]
updated: 2026-07-15
up: "[[06-ingestion/index]]"
reviewed: false
---
# LIFE-009 Semantic Supersession with Abstention

Supersession promises semantic similarity plus topic compatibility. A candidate is the predecessor only when the post-hint content is semantically similar (cosine ≥ 0.88) AND shares at least one topic with the new memory. If zero candidates pass, or two or more candidates pass, the maturation sweep abstains (no supersession inferred).

## Invariant

- Similarity ≥ 0.88 (cosine, dense embedding) on the post-hint content of the new memory and the post-hint content of the candidate.
- Topic match: at least one `linked_to_topics` entry in common between the new memory and the candidate.
- Abstain (return None) on: zero candidates that pass; two or more candidates that pass (ambiguous).
- Bounded candidate search via Qdrant `scroll(limit=max_candidates, ...)` with a default of 20.

## Contract

- The post-hint needle is the content with the supersession hint prefix stripped (e.g., "Correction: the meeting is at 3pm" → "the meeting is at 3pm"). The same prefix-strip is applied to the candidate's content before similarity scoring.
- A candidate with similarity below 0.88 does not match (regardless of substring overlap).
- A candidate with no shared topic does not match (regardless of similarity).
- The "needle" is embedded once; each candidate is embedded once; cosine similarity is computed in-process (no per-candidate network roundtrip).

## Test Contract

1. `test_paraphrase_supersession`
2. `test_correction_supersession_links_to_right_predecessor`
3. `test_negation_supersession_links_to_right_predecessor`
4. `test_participant_change_supersession`
5. `test_time_change_supersession`
6. `test_unrelated_substring_overlap_does_not_supersede`
7. `test_ambiguous_candidates_abstain`
8. `test_no_candidates_abstain`
9. `test_threshold_below_minimum_abstains`
10. `test_predecessor_and_back_link_correctness_in_sweep`
11. `test_retry_idempotency_in_sweep`
12. `test_bounded_candidate_search`
13. `test_substring_only_does_not_match`
14. `test_seam_makes_exactly_one_embed_dense_call` (call-shape discriminator; close-out add)
15. `test_existing_no_predecessor_branch_still_returns_none`
16. `test_existing_both_sides_of_link_still_set`
