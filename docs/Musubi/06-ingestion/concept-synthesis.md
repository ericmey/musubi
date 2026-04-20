---
title: Concept Synthesis
section: 06-ingestion
tags: [concepts, ingestion, lifecycle, section/ingestion, status/research-needed, synthesis, type/spec]
type: spec
status: research-needed
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Concept Synthesis

Daily job that clusters matured episodic memories and generates `SynthesizedConcept` objects describing common themes. Inspired by Mem0's extract-consolidate pattern ([https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)) applied at the concept layer.

See [[04-data-model/synthesized-concept]] for the concept schema; this doc describes how they're made.

## When it runs

- **Daily**, 03:00 local (configurable, `SYNTHESIS_SCHEDULE`).
- Also runs on-demand via `musubi-cli synthesis run --namespace <ns>`.

Concurrency: one synthesis run per namespace at a time. A file lock per `<ns>` in `/srv/musubi/locks/synthesis-<hash>.lock`.

## Inputs

Per namespace:

```
matured episodic memories with updated_epoch > last_synthesis_run_epoch[ns]
```

We track the last run per-namespace in sqlite (`/srv/musubi/lifecycle-state/synthesis-cursor.db`).

## Steps

```
 1. select memories                           ~seconds (Qdrant scroll)
 2. cluster                                    ~seconds (vector + tag)
 3. for each cluster of >= 3:
     a. generate title + summary via LLM      ~2-4s per cluster
     b. check match vs existing concept        ~100ms
     c. create new OR reinforce existing
 4. contradiction detection                    LLM, batched
 5. write concepts in synthesized state        batch upsert
 6. advance cursor
```

Budget: a typical run processes ~50–500 new matured memories and produces 5–30 concepts. Total wall time 1–5 minutes.

### Step 1 — Selection

```python
memories = list(
    scroll(
        client, "musubi_episodic",
        filter=must(
            namespace==ns,
            state=="matured",
            updated_epoch > cursor,
        ),
    )
)
```

If fewer than 3 new memories: skip (nothing to cluster).

### Step 2 — Clustering

Two-stage:

**a. Pre-cluster by shared tags/topics** (cheap):

```python
groups = defaultdict(list)
for m in memories:
    key = frozenset(m.topics or m.tags[:2])
    groups[key].append(m)
```

**b. Within each tag/topic group, cluster by dense similarity**:

- Compute pairwise cosine.
- Threshold-cluster at 0.80 (configurable). Transitive closure; min_cluster_size=3.
- Alternative: HDBSCAN with `min_cluster_size=3`. We prefer threshold clustering for interpretability; HDBSCAN is a future option.

A memory may land in multiple clusters (if it spans topics); that's fine — it'll feed multiple concepts.

### Step 3 — Concept generation

For each cluster with ≥ 3 memories, ask the LLM:

```
Below are {N} related memories (title + content). What common theme emerges?

Return JSON:
{
  "title": "3-10 words, noun phrase",
  "content": "100-500 words of summary. Should be factual and represent the shared theme, NOT restate each memory. Use neutral voice.",
  "rationale": "20-60 words: why these belong together",
  "tags": ["tag1", "tag2", ...],
  "importance": 1-10,
  "contradicts_notice": "" or "<brief note if these memories contradict each other>"
}
```

LLM is Qwen2.5-7B-Instruct via Ollama. Temperature 0.2 — we want consistent synthesis. Output parsed strictly. Parse failure → skip this cluster, log, continue.

### Step 3b — Match vs existing

Before creating a new concept, check for an existing concept in the same namespace that's semantically similar:

```python
existing = query_points(
    "musubi_concept",
    filter=must(namespace == ns, state in ("matured", "promoted")),
    query=new_concept.dense_vector,
    using="dense_bge_m3_v1",
    limit=5,
)
```

If the top result has similarity ≥ 0.85: **reinforce** the existing concept instead of creating a new one.

Reinforcement updates:

- `merged_from`: union with new memory IDs.
- `reinforcement_count`: +1.
- `last_reinforced_at`: now.
- `importance`: max(existing, new).
- No content change (the existing phrasing is kept; new evidence just reinforces it).

### Step 3c — Create new

Otherwise, insert a new `SynthesizedConcept` in `synthesized` state with:

- `merged_from = [m.object_id for m in cluster]` (must be ≥ 3).
- `merged_from_planes = ["episodic"]` (today; future mix).
- `synthesis_rationale = rationale from LLM`.
- `state = "synthesized"` (matures later — see below).

### Step 4 — Contradiction detection

After all concepts are generated in a run, we look at pairs of concepts that overlap semantically but not identically:

```
0.75 <= cosine(A, B) < 0.85 AND namespace(A) == namespace(B)
```

For each such pair, ask the LLM:

```
Are these two concepts CONSISTENT (complementary) or CONTRADICTORY (mutually exclusive)?
Output: {"verdict": "consistent" | "contradictory", "reason": "..."}
```

If contradictory: write `contradicts` links on both concepts; both blocked from promotion until human resolves (see [[04-data-model/synthesized-concept#contradiction-detection]]).

### Step 5 — Write

Batched upsert into `musubi_concept`. Single Qdrant call per run (not per concept).

### Step 6 — Cursor

Update `synthesis-cursor.db`: `last_run_epoch[ns] = max(memory.updated_epoch for memory in selected)`.

## Maturation of concepts

A separate daily job (`concept_maturation`) advances concepts from `synthesized → matured` when:

- `created_epoch < now - 24h` (24 hours since synthesis), AND
- No `contradicts` entries are active.

This gives human review a day to surface objections before a concept becomes queryable in the default-state filter.

## Demotion

Concept demotion job, daily:

- Select `state == "matured"` AND `last_reinforced_at < now - 30d`.
- Transition to `demoted`. Reason: `decay-rule:no-reinforcement`.
- Emit Thought to operator: "Concept X demoted after 30 days without reinforcement."

Demoted concepts are excluded from default retrieval but kept for lineage.

## Idempotency

Running synthesis twice with no new memories → cursor doesn't move, no cluster re-fires, no writes. Idempotent.

Running synthesis twice with the same new memories (cursor reset) → same clusters form, match-vs-existing triggers reinforcement rather than duplication. Non-duplicating.

We test both.

## Failure handling

| Failure | Behavior |
|---|---|
| LLM down (Ollama) | Entire synthesis run skipped; cursor NOT advanced; logged with retry; next run picks up where we left off. |
| LLM returns invalid JSON for a cluster | That cluster skipped; cursor advances past it (we tried); re-evaluated next run if new memories push the cluster above threshold. |
| Qdrant write fails | Entire synthesis run atomic — we collect all concepts, then write in one batch; on batch failure, no partial state. Retry on next run. |
| Contradiction detection LLM fails | Concepts written WITHOUT contradiction links; a separate daily "contradiction-rerun" job re-evaluates. |

## Cost

Per run, typical household-scale:

- ~500 memories → ~30 clusters → ~30 LLM calls for generation + ~60 for contradiction pairs = 90 LLM calls × ~2s = ~3 minutes of Qwen2.5-7B Q4 inference.
- ~1 Qdrant batch write.

Well within an hour's window. Scales linearly with cluster count.

## Test contract

**Module under test:** `musubi/lifecycle/synthesis.py`

Selection:

1. `test_selects_only_matured_since_cursor`
2. `test_skips_when_fewer_than_3_new_memories`
3. `test_cursor_per_namespace_tracked_separately`

Clustering:

4. `test_cluster_by_shared_tags_first`
5. `test_cluster_by_dense_similarity_within_tag_group`
6. `test_cluster_min_size_3_enforced`
7. `test_memory_can_appear_in_multiple_clusters`

Concept generation:

8. `test_llm_prompt_receives_all_cluster_memories`
9. `test_llm_json_parse_failure_skips_cluster`
10. `test_concept_has_min_3_merged_from`
11. `test_concept_starts_in_synthesized_state`

Match vs existing:

12. `test_high_similarity_match_reinforces_existing`
13. `test_low_similarity_creates_new_concept`
14. `test_reinforcement_increments_count_and_merges_sources`

Contradictions:

15. `test_overlapping_concepts_checked_for_contradiction`
16. `test_contradictory_concepts_link_both_sides`
17. `test_contradicted_concept_blocked_from_promotion`

Lifecycle:

18. `test_synthesized_matures_after_24h_without_contradiction`
19. `test_synthesized_blocked_from_maturing_with_contradiction`
20. `test_concept_demotes_after_30d_no_reinforcement`

Failures:

21. `test_ollama_down_does_not_advance_cursor`
22. `test_qdrant_batch_fails_no_partial_state`
23. `test_invalid_json_for_cluster_skipped_not_failed_run`

Property:

24. `hypothesis: synthesis is idempotent across runs with no new memories`
25. `hypothesis: re-running synthesis with same inputs produces same number of concepts (not duplicated)`

Integration:

26. `integration: real Ollama, 100 synthetic memories in 5 clusters → 5 concepts, each ≥ 3 merged_from`
27. `integration: contradiction flow — inject two contradictory memory clusters, both concepts end up with symmetric contradicts links`
