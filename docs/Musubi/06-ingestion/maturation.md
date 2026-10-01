---
title: Maturation
section: 06-ingestion
tags: [ingestion, lifecycle, maturation, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: "tests/lifecycle/test_maturation.py"
---
# Maturation

Moving episodic memories from `provisional` to `matured`. An hourly lifecycle job and the first enrichment step.

Code: `src/musubi/lifecycle/maturation.py` (sweeps, `MaturationConfig`, job builder) and `src/musubi/llm/ollama.py` (the LLM client).

## Why maturation exists

At capture time we have the content, the tags the client sent, and an uncalibrated importance guess. We do not have:

- A calibrated importance score.
- Normalized tags (tag sprawl breaks filtering).
- Topic inference (most captures arrive without topics).
- Confirmation that the memory is not noise.

Maturation resolves these over time. Retrieval defaults to `matured` rows.

## Schedule

**Hourly at `:13` UTC** (`maturation_episodic`), with the provisional-TTL sweep at **`:17`** (`provisional_ttl`). The lifecycle worker's tick loop dispatches both; see [[06-ingestion/lifecycle-engine]].

Concurrency: one run at a time per job, enforced by an `flock` on `<lock dir>/maturation_episodic.lock` (and `provisional_ttl.lock`), where the lock dir is `locks/` next to `LIFECYCLE_SQLITE_PATH`. A second run that cannot take the lock logs and skips.

## Selection

```sql
-- conceptual; the real query is a Qdrant payload-filter scroll
SELECT * FROM musubi_episodic
WHERE state = 'provisional'
  AND created_epoch < now - 3600
  AND updated_epoch > <cursor>
LIMIT 500
```

`MaturationConfig` defaults:

- **Age floor**: `min_age_sec = 3600`. Younger rows may still be deduped or reinforced.
- **Batch size**: `batch_size = 500`. Bounds per-run cost.
- **Cursor**: the largest processed `updated_epoch`, stored in the shared lifecycle SQLite database, so a crash resumes after the last committed row.

These are constructor arguments to `build_maturation_jobs(config=...)`. They are not environment settings.

## Per-memory pipeline

```
 1. rule-normalize tags                          (local)
 2. LLM importance rescore                       (batched, 10 items per call)
 3. LLM topic inference                          (batched, 10 items per call)
 4. optional supersession inference              (embedder + Qdrant)
 5. transition to matured                        (via transition())
 6. write enrichment fields                      (importance, tags, linked_to_topics)
```

Steps 2 and 3 go to the **lifecycle LLM**, which the deployment selects (ADR 0043, [[13-decisions/0043-lifecycle-llm-openai-compatible-endpoint]]):

- `LIFECYCLE_LLM_API`: `ollama` (default; native `/api/chat` with a JSON-schema `format`) or `openai` (`/v1/chat/completions` with `response_format: json_schema`).
- `LIFECYCLE_LLM_BASE_URL`: defaults to `OLLAMA_URL`.
- `LIFECYCLE_LLM_MODEL`: defaults to `LLM_MODEL`.
- `LIFECYCLE_LLM_API_KEY`: optional bearer key.

With `openai`, base URL and model must both be set explicitly. Calls run at temperature 0 and every response is validated against a pydantic model before use.

### Importance

Prompt: `src/musubi/llm/prompts/importance/v1.txt`. Output is strict JSON keyed by a per-row correlation id. A missing or invalid result keeps the captured importance. A rescore stamps `importance_last_scored_at` / `importance_last_scored_epoch`.

### Tag normalization

Rule-based, no LLM:

- Lowercase, strip whitespace, spaces to hyphens.
- Apply the alias map (`DEFAULT_TAG_ALIASES` in `maturation.py`: `nvidia-gpu` to `nvidia`, `gpu-setup` to `gpu`).
- Drop empty strings and dedupe.

The alias map is a code default overridable through `MaturationConfig.tag_aliases`. There is no file-based alias loader. Unknown tags pass through unchanged.

### Topic inference

Prompt: `src/musubi/llm/prompts/topics/v1.txt`. The model assigns 0–3 lowercase `area/subarea` topics per item (for example `infrastructure/gpu`), prefers topics already on the item, and returns an empty list when nothing fits confidently. There is no curated topic taxonomy file; topics are free-form within that format. Results land in `linked_to_topics`.

### Supersession inference

Only for content that starts with a hint prefix (`Update:`, `Correction:`, `Replacing:`, case-insensitive). The sweep looks for a previous memory in the same namespace with dense cosine similarity **≥ 0.88** that shares at least one topic, and abstains when the match is ambiguous (see [[06-ingestion/life009-semantic-supersession]]). On a match it links both sides (`supersedes` on the new row; the old row transitions to `superseded`) and records lifecycle events for both. Without a hint, nothing is inferred.

### Transition

Via the typed transition function (see [[04-data-model/lifecycle#transition-function]]):

```python
transition(
    client,
    object_id=mem.object_id,
    target_state="matured",
    actor="lifecycle-worker",
    reason="maturation-sweep",
    lineage_updates=LineageUpdates(supersedes=supersedes_inferred),
)
```

Enrichment fields are written after the transition succeeds, through the fenced payload-patch path. They are not state changes and are not audited one by one. A row that changed or was leased after selection is refused rather than overwritten.

## Provisional TTL

Rows still `provisional` after **7 days** (`provisional_ttl_sec`) are archived, not deleted:

```
WHERE state = 'provisional' AND created_epoch < now - 7*86400
-> transition(target_state='archived', reason='provisional-ttl')
```

Seven days is 168 maturation runs. A row still provisional by then is almost certainly a capture error, an orphan, or an LLM-outage casualty. Archiving keeps it for review.

## Failure modes

### Lifecycle LLM unavailable

The client returns `None` on any connect error, timeout, non-2xx, bad JSON or validation failure. Then:

- Importance keeps the captured value.
- Topics keep whatever `linked_to_topics` the row already had.
- Supersession inference still runs; it needs the embedder, not the LLM.
- The state transition **still happens**. An unenriched matured memory beats a stuck provisional one.

There is **no re-enrichment sweep.** `MaturationConfig.importance_reenrich_age_sec` exists but nothing reads it, so a row matured during an outage keeps its captured importance. (Not implemented.)

### Partial batch failure

Each batch of 10 is isolated. A failed batch falls back as above while every other batch's enrichment lands. Failed batches increment `musubi_lifecycle_enrichment_batch_failures_total{kind}` (`kind` is `importance` or `topics`), so sustained degradation shows up as a metric, not only as a log line. Results are matched by correlation id; an item missing from a successful response falls back the same way.

### Raw-response debugging

`HttpxOllamaClient` can write failed raw responses to a debug directory, but no setting enables it in the shipped configuration. (Not configurable via environment.)

## Throughput

One run handles at most 500 rows: 50 importance calls plus 50 topic calls. Wall time depends on the model and endpoint you configure. ADR 0043 records why: structured-output reliability scales with model capability, so a deployment may trade per-call latency for a larger model.

## Test Contract

**Module under test:** `src/musubi/lifecycle/maturation.py`, `src/musubi/llm/ollama.py`

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
11. `test_batched_call_isolates_failed_batches`
12. `test_batched_call_all_batches_failing_returns_empty_not_none`

Supersession:

13. `test_supersession_inferred_from_hint_keyword`
14. `test_supersession_not_inferred_without_hint`
15. `test_supersession_sets_both_sides_of_link`

Transitions:

16. `test_state_transitions_to_matured`
17. `test_transition_uses_typed_function`
18. `test_lifecycle_event_emitted`
19. `test_ollama_outage_still_matures_without_enrichment`

TTL:

20. `test_provisional_older_than_7d_archived`
21. `test_archival_emits_lifecycle_event`

Concurrency:

22. `test_file_lock_prevents_double_execution`

Property (skipped, declared out of scope):

23. `hypothesis: no matured memory has created_epoch in the future`
24. `hypothesis: provisional memories older than 7d are always archived after one sweep`

Integration (skipped, needs a live LLM endpoint):

25. `integration: real LLM, 50 synthetic provisional memories mature in one sweep, importance distribution is plausible`
26. `integration: LLM-offline scenario — maturation completes without enrichment`
