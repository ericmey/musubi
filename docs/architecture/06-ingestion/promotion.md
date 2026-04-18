---
title: Promotion
section: 06-ingestion
tags: [curated, ingestion, lifecycle, promotion, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Promotion

Turning a well-reinforced concept into a durable curated-knowledge file in the Obsidian vault. The crown jewel of the write path.

See [[04-data-model/synthesized-concept#promotion-gate]] and [[04-data-model/curated-knowledge]].

## When it runs

- **Daily**, 04:00 local (after synthesis at 03:00).
- On-demand via `musubi-cli promotion run --namespace <ns> --concept <id>`.

Concurrency: one promotion sweep at a time.

## Gate

A concept is eligible when all are true:

- `state == "matured"` (not synthesized, not demoted).
- `reinforcement_count >= 3` (`PROMOTION_REINFORCEMENT_THRESHOLD`).
- `importance >= 6` (`PROMOTION_IMPORTANCE_THRESHOLD`).
- `created_at < now - 48h` (buffer for contradictions).
- No active `contradicts` entries of equal-or-higher `provenance_strength`.
- `promotion_attempts < 3` (anti-infinite-retry).
- No existing curated in same namespace with `promoted_from == concept.object_id` (already promoted).

The sweep selects candidates via Qdrant payload filters; checks the remaining conditions in Python.

## The write path

```
 1. select eligible concepts                  Qdrant scroll
 2. for each:
     a. render markdown body via LLM          ~3s
     b. validate rendering                    strict pydantic
     c. compute vault path                    deterministic from tags/topics
     d. check for path conflict               vault file system
     e. write write-log entry (core-wrote)    sqlite
     f. write markdown file                    vault fs
     g. write body_hash + frontmatter id      in step f
     h. upsert musubi_curated Qdrant point     direct
     i. transition concept to 'promoted'       typed transition
     j. set concept.promoted_to = new KSUID    payload
     k. set curated.promoted_from = concept_id payload (already in step h)
     l. emit LifecycleEvent (both sides)
     m. emit Thought to eric/* on channel ops-alerts
```

Steps a–m are per-concept; concurrency within a sweep is bounded (1 at a time by default — promotion is a careful, human-reviewable action). Configurable to 4-way parallel (`PROMOTION_CONCURRENCY`).

### Step 2a — Rendering

LLM prompt (Qwen2.5-7B, low temperature):

```
You are writing a curated knowledge note for an Obsidian vault. 
Source: a synthesized concept derived from {N} episodic memories.

Concept title: {title}
Concept content: {content}
Synthesis rationale: {rationale}
Top contributing memories (summaries): {...}

Write a markdown document suitable for a long-lived curated knowledge file.
Guidelines:
- Title as H1.
- 200-800 word body, markdown formatted.
- Include a "Background" section summarizing why this is worth recording.
- Include a "Details" section with the key facts.
- Use [[wikilinks]] to other topics when relevant (list provided).
- Do NOT invent facts. Only state what's in the provided material.
- Do NOT include frontmatter — we add that separately.

Output the markdown body only.
```

The rendering is distinct from the concept's `content` — the concept is a machine summary; the curated doc is human-consumable prose.

### Step 2b — Validate rendering

```python
class PromotionRender(BaseModel):
    body: str = Field(min_length=100, max_length=20000)
    wikilinks: list[str]
    sections: list[str]               # heading titles
```

Validation:

- Body length bounds.
- Must contain at least one H2 ("proper document structure").
- Doesn't contain "As an AI model" or other meta disclaimers (regex blacklist).
- Wikilink targets are in the allowed topic set.

If validation fails: retry up to 2 times with corrective prompts, then abort this concept's promotion (increments `promotion_attempts`).

### Step 2c — Vault path

```python
def compute_path(concept) -> str:
    primary_topic = concept.topics[0] if concept.topics else "_misc"
    slug = slugify(concept.title)
    return f"curated/{namespace_to_dir(concept.namespace)}/{primary_topic}/{slug}.md"
```

E.g., for namespace `eric/_shared/concept` and primary topic `infrastructure/gpu`, title "CUDA 13 driver 575":

```
curated/eric/_shared/infrastructure/gpu/cuda-13-driver-575.md
```

Path is deterministic from concept metadata. Renames happen later (human rename in Obsidian).

### Step 2d — Path conflict

If the target path already exists:

- If the file's `musubi-managed: true` and `promoted_from == this concept`: re-write (idempotent re-promotion).
- If the file's `musubi-managed: true` and `promoted_from != this concept`: conflict. Write sibling: `<slug>-v2.md`. Log a warning Thought.
- If the file's `musubi-managed: false` (human-authored): conflict. Write sibling: `<slug>-promoted-<short-ksuid>.md`. Log; operator can merge later.

### Step 2e–f — Write-log + file write

Write-log prevents the Vault Watcher from re-indexing our own write:

```python
with write_log.session() as sess:
    sess.add(WriteLogEntry(
        file_path=path,
        body_hash=body_hash,
        written_by="core",
        written_at=time.time(),
    ))
    sess.commit()
    # Now write the file
    write_file_atomically(path, frontmatter + "\n" + body)
```

Atomic file write: write to `<path>.tmp`, then `os.rename()` to `<path>`. No half-written files.

### Step 2h — Qdrant upsert

Direct insert of the `musubi_curated` point (not via Vault Watcher). This avoids a race where the caller polls for the promotion result before the Watcher gets around to indexing.

The Vault Watcher will eventually see the filesystem event for our write, look up the write-log, find our entry, and skip re-indexing. See [[06-ingestion/vault-sync#echo-prevention]].

### Step 2i-k — Bidirectional linkage

Atomic-ish in a single `batch_update_points` call:

- Concept: `state=promoted`, `promoted_to=<curated ksuid>`, `promoted_at=now`.
- Curated: `promoted_from=<concept ksuid>`, `promoted_at=now`. (Set at step h, this is just a safety re-set.)

If Qdrant updates succeed but the file write failed (unlikely given step f's atomicity): detected at reconcile time; the reconciler drops the orphan Qdrant point.

### Step 2m — Operator notification

A Thought on channel `ops-alerts`:

```
to_presence: "all"
from_presence: "lifecycle-worker"
channel: "ops-alerts"
content: "Promoted concept '{title}' to curated/... Please review."
importance: 7
```

The operator (human) sees this in their Obsidian vault's inbox or via any presence checking ops-alerts.

## Rejection / Promotion failure

If promotion fails for any concept (rendering validation, path conflict, LLM repeated failure):

- `promotion_attempts += 1`.
- `promotion_rejected_at = now`.
- `promotion_rejected_reason = "..."`.
- Emit Thought on `ops-alerts` with the reason.
- Concept remains in `matured` state, still eligible for retry on next run — unless `promotion_attempts == 3`, in which case it stays "matured" forever until human intervention (we stop trying).

## Human override

```bash
# Force promotion with custom rendering
musubi-cli promotion write --concept <id> --body-file ./draft.md

# Reject permanently
musubi-cli concept reject --concept <id> --reason "superseded by existing curated"
```

`concept reject` marks `promotion_attempts = MAX`, sets `promotion_rejected_at`, and demotes the concept to `demoted` (not queryable by default).

## Rollback

If we promoted the wrong concept (bad synthesis), manual rollback:

1. `musubi-cli curated archive <curated-id>` — soft-deletes the file (moves to `_archive/`), marks point `state=archived`.
2. `musubi-cli concept demote <concept-id>` — transitions back to `demoted`.
3. Both emit LifecycleEvents.

Rollback is purely metadata + file-move. The audit log keeps the full history.

## Test contract

**Module under test:** `musubi/lifecycle/promotion.py` + `musubi/vault/writer.py`

Gate:

1. `test_gate_requires_matured_state`
2. `test_gate_requires_reinforcement_gte_3`
3. `test_gate_requires_importance_gte_6`
4. `test_gate_requires_age_gte_48h`
5. `test_gate_blocks_on_active_contradiction`
6. `test_gate_blocks_after_3_attempts`
7. `test_gate_skips_already_promoted`

Rendering:

8. `test_llm_renders_markdown_body`
9. `test_rendering_validation_rejects_short_body`
10. `test_rendering_validation_rejects_missing_h2`
11. `test_rendering_retry_corrective_prompt`

Path:

12. `test_path_derived_from_topic_and_title`
13. `test_path_conflict_with_same_concept_rewrites_in_place`
14. `test_path_conflict_with_other_concept_writes_sibling`
15. `test_path_conflict_with_human_file_writes_sibling_and_logs`

Write-log:

16. `test_writelog_entry_precedes_file_write`
17. `test_file_written_atomically`
18. `test_watcher_sees_writelog_and_skips_reindex`

Qdrant:

19. `test_curated_point_upserted_with_promoted_from`
20. `test_concept_state_set_to_promoted`
21. `test_bidirectional_links_set_in_single_batch`

Notification:

22. `test_lifecycle_events_emitted_for_both_sides`
23. `test_thought_emitted_to_ops_alerts`

Failure:

24. `test_promotion_rejected_after_3_attempts_stops_retrying`
25. `test_rendering_failure_increments_attempts_not_promotes`

Concurrency:

26. `test_concurrent_promotion_of_different_concepts_ok`
27. `test_concurrent_promotion_of_same_concept_one_wins`

Human override:

28. `test_cli_force_promote_with_custom_body`
29. `test_cli_reject_sets_rejected_fields_and_demotes`

Property:

30. `hypothesis: every successful promotion produces exactly one curated file and one Qdrant point`

Integration:

31. `integration: happy path — 1 concept → 1 file in vault/, 1 point in musubi_curated, both linked, ops-alert present`
32. `integration: path conflict with human file — sibling created, no human file modified`
33. `integration: rollback flow — promote then archive, vault file in _archive/, Qdrant state=archived`
