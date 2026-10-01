---
title: Concept Synthesis
section: 06-ingestion
tags: [concepts, ingestion, lifecycle, section/ingestion, status/complete, synthesis, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["src/musubi/lifecycle/synthesis.py", "tests/lifecycle/test_synthesis.py"]
---
# Concept Synthesis

A daily job that clusters matured episodic memories and generates `SynthesizedConcept` objects describing their common themes. It follows Mem0's extract-consolidate pattern ([https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)), applied at the concept layer.

See [[04-data-model/synthesized-concept]] for the concept schema; this page covers how concepts are made. Code: `src/musubi/lifecycle/synthesis.py`.

## When it runs

- **Daily at 03:00 UTC** (`synthesis` job). The time is fixed in code; there is no schedule setting.
- **On demand:** `scripts/force_synthesis.py` runs the same `synthesis_run` path for every identity family, or one family via `MUSUBI_FORCE_FAMILY`. Run it inside the core container. Without `MUSUBI_FORCE_SYNTHESIS_CONFIRM=1` it is a dry run that only lists families.
- `POST /v1/ops/debug/trigger-synthesis` (operator scope) is an **integration-test hook**. It only runs with `simulate_ollama_offline: true` and returns `501` otherwise, so it cannot produce real concepts.

Concurrency: one sweep at a time, via `<lock dir>/synthesis.lock`. Inside a sweep, identity families run one after another.

## Scope: identity families

Synthesis runs per **identity family**, the tenant segment of a namespace (`alex` for `alex/voice/episodic` and `alex/claude-code/episodic`). Each tick discovers families from the episodic collection's `identity_family` payload, falling back to the namespace prefix for rows not yet backfilled. A family's memories cluster together regardless of which presence captured them.

New concepts are written to `<family>/shared/concept`. Matching against existing concepts covers the whole family, so a concept in another presence's concept namespace can be reinforced.

One family's failure does not stop the others. It increments `musubi_lifecycle_synthesis_family_failures_total{family}`.

## Inputs

Per family, the shared lifecycle SQLite database holds two things:

1. **Cursor:** the high-water mark of `updated_epoch` already scanned. It is a performance shortcut, not a correctness gate.
2. **Candidate pool:** memories seen but not yet clustered. They stay eligible for `candidate_ttl_sec` (30 days), so slow-forming patterns can still cluster when peer memories arrive later. A successful cluster removes its members from the pool; rows past the TTL are pruned.

Each run pulls matured episodics above the cursor, plus the current candidates. With fewer than 3 memories in total, it records them as candidates and stops.

## Steps

```
 1. select new matured memories + carried-forward candidates     Qdrant scroll
 2. cluster: group by topic/tag, then dense-similarity components
 3. for each cluster of >= 3:
     a. generate title/content/rationale/tags/importance via LLM
     b. match against existing concepts in the family
     c. reinforce the existing concept OR create a new one
 4. contradiction check across this run's concepts               LLM
 5. maintain the candidate pool
 6. advance the cursor
```

### Clustering

Two stages:

1. **Group** by `linked_to_topics`, or by the first two tags when a memory has no topics. A memory with several keys joins several groups, so it can feed more than one concept.
2. **Within each group**, connect pairs with dense cosine ≥ `cluster_threshold` (**0.70**) and take connected components (transitive closure). Components smaller than `min_cluster_size` (3) are dropped. A concept, by definition, is a pattern across at least three observations.

Threshold clustering was chosen over HDBSCAN for interpretability.

### Concept generation

Prompt: `src/musubi/llm/prompts/synthesis/v1.txt`. The model returns:

```json
{"title": "3-8 words", "content": "one or two sentences", "rationale": "why these cluster",
 "tags": ["area/sub", "..."], "importance": 1, "contradicts_notice": ""}
```

The call goes to the **lifecycle LLM** that the deployment selects (ADR 0043): `LIFECYCLE_LLM_API` is `ollama` (native, JSON-schema `format`) or `openai` (any OpenAI-compatible endpoint with strict `json_schema` output), with `LIFECYCLE_LLM_BASE_URL`, `LIFECYCLE_LLM_MODEL` and `LIFECYCLE_LLM_API_KEY`. Temperature is 0, and output is validated strictly.

Choose a model that reliably produces this schema. ADR 0043 records that a small (4B-class) local model produced zero valid synthesis outputs over two nightly passes. This is the hardest structured-output task in the lifecycle, so a deployment may need a larger model here. A parse failure or `None` skips that cluster and the run continues.

An oversized cluster (more than `max_llm_cluster_members`, default 20) is synthesized from a deterministic importance-first sample (KSUID tiebreak), not sent whole. Threshold clustering has no size ceiling, and a mega-cluster's prompt would overrun a small model's context on every attempt.

### Match against existing concepts

The new concept's title and rationale are embedded and compared with existing concepts in the same family (`state IN ("matured", "promoted")`). If the top match has similarity ≥ `match_threshold` (**0.85**), the existing concept is **reinforced** with each cluster member (`ConceptPlane.reinforce`): sources are merged, `reinforcement_count` and `last_reinforced_at` are updated, and the existing wording is kept.

### Create new

Otherwise a new `SynthesizedConcept` is created in `<family>/shared/concept` in state `synthesized`, with `merged_from` set to the cluster's memory ids (at least 3), the LLM's `synthesis_rationale`, `tags` and `importance`.

### Contradiction detection

For each pair of concepts created or reinforced **in this run** whose content embeddings fall in `0.75 <= cosine < 0.85`, the LLM is asked whether they are consistent or contradictory. If contradictory, `contradicts` links are written on both sides. Both concepts are then blocked from maturing and from promotion until the contradiction is resolved (see [[04-data-model/synthesized-concept#contradiction-detection]]).

## Concept maturation

A separate daily job (`concept_maturation`, 03:30 UTC, in `src/musubi/lifecycle/maturation.py`) moves a concept from `synthesized` to `matured` when:

- it was created more than **24 hours** ago;
- `reinforcement_count >= 3`;
- `contradicts` is empty.

This gives review a day to surface objections before the concept appears under the default state filter.

## Demotion

Concept demotion (`demotion_concept`, daily 05:00 UTC) demotes matured concepts with no reinforcement in 30 days. See [[06-ingestion/demotion]].

## Idempotency

- Running twice with no new memories: nothing new above the cursor, the candidates have already been tried, no writes.
- Re-running over the same memories (for example after a cursor reset): the same clusters form, and match-against-existing reinforces rather than duplicates.

## Failure handling

| Failure | Behaviour |
|---|---|
| LLM unavailable, or invalid JSON / `None` for a cluster | That cluster is skipped and the run continues. Its members stay in the candidate pool and are retried next run; the cursor still advances. This is deliberate: when a failure froze the cursor, one permanently failing cluster could livelock a family. The accepted consequence is that an outage longer than `candidate_ttl_sec` (30 days) ages candidates out. |
| Candidate row fails model validation (schema drift) | Skipped per row and logged with its id, counted as `candidates_decode_failed` on the run report and in `musubi_lifecycle_synthesis_decode_skips_total`. A non-zero count means a degraded run, not a failed one. |
| Cluster larger than `max_llm_cluster_members` | Synthesized from the importance-first sample; unsampled members can reinforce the concept on later runs. |
| Qdrant write fails mid-run | Concepts are written one at a time, so earlier clusters stay persisted. The run is safe to repeat but not atomic. |
| Contradiction LLM call fails | The pair is left unlinked. No separate job re-checks it. |
| Unexpected exception for a family | The family is aborted, the failure metric is incremented, and the remaining families run. |

## Cost

LLM calls per run are roughly one per cluster plus one per in-band concept pair. Wall time depends on the configured model and endpoint. Qdrant writes are one per created or reinforced concept.

## Test Contract

**Module under test:** `src/musubi/lifecycle/synthesis.py`

Selection and cursor:

1. `test_selects_only_matured_since_cursor`
2. `test_skips_when_fewer_than_3_new_memories`
3. `test_cursor_per_namespace_tracked_separately`
4. `test_cursor_get_set_accepts_namespace_or_family`

Candidate pool:

5. `test_candidates_upsert_and_get_within_ttl`
6. `test_candidates_filtered_by_ttl_window`
7. `test_candidates_remove_on_successful_cluster`
8. `test_candidates_pruned_after_ttl`
9. `test_candidates_per_family_isolation`
10. `test_cursor_skip_fix_unclustered_memories_carry_forward`

Clustering:

11. `test_cluster_by_shared_tags_first`
12. `test_cluster_by_dense_similarity_within_tag_group`
13. `test_cluster_min_size_3_enforced`
14. `test_memory_can_appear_in_multiple_clusters`

Concept generation:

15. `test_llm_prompt_receives_all_cluster_memories`
16. `test_llm_json_parse_failure_skips_cluster`
17. `test_concept_has_min_3_merged_from`
18. `test_concept_starts_in_synthesized_state`

Match against existing:

19. `test_high_similarity_match_reinforces_existing`
20. `test_low_similarity_creates_new_concept`
21. `test_reinforcement_increments_count_and_merges_sources`

Contradictions:

22. `test_overlapping_concepts_checked_for_contradiction`
23. `test_contradictory_concepts_link_both_sides`
24. `test_contradiction_updates_are_isolated_by_namespace`
25. `test_contradicted_concept_blocked_from_promotion` (skipped here; covered by `tests/lifecycle/test_promotion.py::test_gate_blocks_on_active_contradiction`)

Lifecycle:

26. `test_synthesized_matures_after_24h_without_contradiction`
27. `test_synthesized_blocked_from_maturing_with_contradiction`
28. `test_concept_demotes_after_30d_no_reinforcement`

Failures and robustness:

29. `test_ollama_down_keeps_memories_eligible_via_candidates`
30. `test_llm_none_skips_cluster_not_entire_run`
31. `test_invalid_json_for_cluster_skipped_not_failed_run`
32. `test_mega_cluster_sampled_to_llm_cap`
33. `test_oversized_clusters_deduplicate_after_sampling`
34. `test_inline_vector_row_with_layout_fields_synthesizes`
35. `test_one_undecodable_row_degrades_run_without_aborting_family`
36. `test_family_exception_increments_failure_metric_without_reraise`
37. `test_qdrant_batch_fails_no_partial_state` (skipped; writes are not batch-atomic)

Family discovery:

38. `test_discover_returns_identity_families_not_full_namespaces`
39. `test_discover_paginates_until_offset_none`
40. `test_discover_returns_empty_on_scroll_exception`
41. `test_discover_falls_back_to_namespace_prefix_when_identity_family_missing`

Skipped (property and integration):

42. `hypothesis: synthesis is idempotent across runs with no new memories`
43. `hypothesis: re-running synthesis with same inputs produces same number of concepts (not duplicated)`
44. `integration: real LLM, 100 synthetic memories in 5 clusters → 5 concepts, each ≥ 3 merged_from`
45. `integration: contradiction flow — inject two contradictory memory clusters, both concepts end up with symmetric contradicts links`
