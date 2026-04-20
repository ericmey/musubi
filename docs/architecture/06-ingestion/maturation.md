---
title: Maturation
section: 06-ingestion
tags: [ingestion, lifecycle, maturation, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
implements: "tests/lifecycle/test_maturation.py"
---
# Maturation

Promoting episodic memories from `provisional` to `matured`. Hourly background job. The first enrichment step.

## Why maturation exists

At capture time, we have minimal information: content, tags the adapter provided, maybe a topic guess. We don't have:

- A reliable importance score (the adapter's guess is uncalibrated).
- Normalized tags (tag sprawl would break filtering).
- Topic inference (many captures don't come with topics).
- Confirmation that the memory isn't noise.

Maturation resolves these over time. Memories that survive the sweep are considered index-worthy — retrieval filters default to `matured` only.

## Schedule

**Hourly**, at `:13` past each hour (arbitrary offset to avoid clashing with clock-edge cron jobs).

Concurrency: one instance at a time, enforced by a file lock at `/srv/musubi/locks/maturation.lock`. See [[06-ingestion/lifecycle-engine]].

## Selection

```sql
-- conceptual; actual query is Qdrant scroll
SELECT * FROM musubi_episodic
WHERE state = 'provisional'
  AND created_epoch < now - 3600
LIMIT 500
```

Parameters:

- **Age floor**: 1 hour (`MATURATION_MIN_AGE_SEC`). We don't mature memories younger than this — they might still be getting deduped / reinforced.
- **Batch size**: 500 (`MATURATION_BATCH`). Bounds per-run cost.
- **Cursor**: `updated_epoch` of the last processed item — resume across crashes.

## Per-memory pipeline

For each selected memory:

```
 1. fetch full content                             (in hand from the scroll)
 2. LLM importance rescore                         ~200-500ms per item
 3. tag normalization                              <10ms
 4. topic inference                                ~200-500ms per item (LLM)
 5. optional supersession detection                ~100ms
 6. transition to matured + write                  ~5ms
 7. emit LifecycleEvent                            <1ms
```

Steps 2 and 4 go through Ollama (see [[08-deployment/gpu-inference-topology]]). We **batch multiple memories per LLM call** (10 at a time in a single prompt) to amortize per-call overhead.

### Step 2 — Importance

Prompt:

```
You are calibrating the importance of memory items (1-10).
Guidelines: ...
For each of the 10 items below, output: {"id": <ksuid>, "importance": <int>, "reason": "<<=20 words>"}.
```

Output is JSON; parsed strictly. If parse fails, fall back to the captured importance (no update).

### Step 3 — Tag normalization

Rule-based (no LLM):

- Lowercase.
- Strip whitespace.
- Convert spaces → hyphens.
- Canonicalize against a small alias dictionary (`nvidia-gpu` → `nvidia`, `gpu-setup` → `gpu`).
- Dedupe within the list.

Alias dictionary lives in `config/tag-aliases.yaml` and is editable. Unknown tags pass through unchanged.

### Step 4 — Topic inference

Prompt (batch of 10):

```
Classify each memory into 0-3 topics from this taxonomy:
<topics dictionary inline or as tool>
If no confident topic applies, return [].
Output JSON: {"id": <ksuid>, "topics": [<topic>, ...]}
```

Topics are hierarchical strings (e.g., `infrastructure/gpu`). Topic taxonomy lives in the vault (`vault/_meta/topics.yaml`) — humans manage it.

### Step 5 — Optional supersession detection

For memories tagged `#supersedes` or clearly marked "update" (heuristic: content starts with "Update:", "Correction:", "Replacing:"), we check for a previous memory in the same namespace with high semantic similarity (≥ 0.88) and the same topic. If found:

- Set `supersedes: [old_id]` on the new memory.
- Set `superseded_by: new_id` on the old memory (transition to `superseded`).
- Emit LifecycleEvent for both.

If not found, we don't infer supersession — it's a conservative step.

### Step 6 — Transition

Via the typed transition function (see [[04-data-model/lifecycle#transition-function]]):

```python
transition(
    client,
    object_id=mem.object_id,
    target_state="matured",
    actor="lifecycle-worker",
    reason="maturation-sweep",
    lineage_updates=LineageUpdates(
        supersedes=supersedes_inferred,
    ),
)
```

Updates `importance`, `tags`, `topics` alongside state in a single `update_points` call. Batched across the current sweep iteration (see [[00-index/agent-guardrails]] on batching).

## Provisional TTL

Memories that remain `provisional` for more than **7 days** are archived (not deleted):

```python
# Hourly, in the same worker, separate select:
WHERE state = 'provisional' AND created_epoch < now - 7*86400
→ transition(state='archived', reason='provisional-ttl')
```

Rationale: 7 days is enough for maturation to have run 168 times. A memory still provisional that long is almost certainly a capture error, orphan, or Ollama-outage casualty. Archiving (not deleting) preserves it for forensic review.

## Failure modes

### Ollama down

- Importance rescore: skip (keep the captured value).
- Topic inference: skip (leave topics empty).
- Supersession detection: skip.
- State transition: **still happens** (we don't block maturation on enrichment being available — an unenriched matured memory is still better than a stuck provisional one; the next sweep will re-enrich, see below).

### Re-enrichment on next sweep

We can't re-select `matured` memories via the normal query (they'd never be picked up again). So we also run a secondary sweep:

```
WHERE state = 'matured' AND (importance_last_scored_at IS NULL OR importance_last_scored_at < now - 7d)
LIMIT 100
```

This re-enriches older matured memories every week, catching any that went through during Ollama outages.

### Parse errors

LLM output JSON parse failures are logged with the raw response in a debug directory (`/srv/musubi/maturation-debug/`) for a week, then rotated. Memory is marked `matured` with the captured importance; logged as a soft failure.

### Partial batch failure

If the LLM returns 8 items instead of 10: match by ID, update the 8, log the missing 2, they'll be re-selected next hour (state is still `provisional`).

## Throughput

At our capture rate (~500/day episodic typical, ~5000/day peak), an hourly sweep with batch=500 is well-sized. Per-run time on Ollama:

- 500 items / 10 per batch = 50 LLM calls
- ~400ms per call on Qwen2.5-7B Q4 via Ollama
- ~20s LLM time per run, +few seconds Qdrant writes = ~25s total

Well within the one-run-per-hour window.

## Test Contract

**Module under test:** `musubi/lifecycle/maturation.py`

Selection:

1. `test_selects_only_provisional_older_than_min_age`
2. `test_batch_size_limits_selection`
3. `test_cursor_resumes_across_runs`

Enrichment:

4. `test_importance_rescored_via_llm`
5. `test_importance_fallback_on_ollama_unavailable`
6. `test_tags_normalized_lowercase_and_hyphenated`
7. `test_tag_aliases_applied`
8. `test_tags_deduped`
9. `test_topics_inferred_from_llm`
10. `test_topics_empty_on_unknown`

Supersession:

11. `test_supersession_inferred_from_hint_keyword`
12. `test_supersession_not_inferred_without_hint`
13. `test_supersession_sets_both_sides_of_link`

Transitions:

14. `test_state_transitions_to_matured`
15. `test_transition_uses_typed_function`
16. `test_lifecycle_event_emitted`
17. `test_ollama_outage_still_matures_without_enrichment`

TTL:

18. `test_provisional_older_than_7d_archived`
19. `test_archival_emits_lifecycle_event`

Concurrency:

20. `test_file_lock_prevents_double_execution`

Property:

21. `hypothesis: no matured memory has created_epoch in the future`
22. `hypothesis: provisional memories older than 7d are always archived after one sweep`

Integration:

23. `integration: real Ollama, 50 synthetic provisional memories mature in one sweep, importance distribution is plausible`
24. `integration: ollama-offline scenario — maturation completes without enrichment, re-enrichment sweep picks them up later`
