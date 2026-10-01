---
title: Promotion
section: 06-ingestion
tags: [curated, ingestion, lifecycle, promotion, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["src/musubi/lifecycle/promotion.py", "src/musubi/llm/promotion_client.py", "tests/lifecycle/test_promotion.py"]
---
# Promotion

Turning a well-reinforced concept into a durable curated-knowledge file in the vault. It is the last step of the write path, and the one a human is expected to review.

See [[04-data-model/synthesized-concept#promotion-gate]] and [[04-data-model/curated-knowledge]].

## When it runs

- **Daily at 04:00 UTC** (`promotion` job), after synthesis (03:00) and concept maturation (03:30).
- **Operator force:** `musubi promote force <concept-id> --namespace <ns> --curated-id <curated-id>` wraps `POST /v1/concepts/{id}/promote`. It links the concept to a curated row that **already exists** (create one first with `POST /v1/curated`). It does not call the LLM.

Concurrency: one sweep at a time (`<lock dir>/promotion.lock`). Each sweep promotes at most `batch_size` concepts (default 1), one after another.

## Gate

A concept is eligible when all of these hold (`_is_eligible` in `src/musubi/lifecycle/promotion.py`):

- `state == "matured"`.
- `reinforcement_count >= 3` (`PROMOTION_REINFORCEMENT_THRESHOLD`).
- `importance >= 6` (`PROMOTION_IMPORTANCE_THRESHOLD`).
- Created at least **48 hours** ago (a buffer for contradictions to surface).
- `contradicts` is empty. Any recorded contradiction blocks promotion.
- `promotion_attempts < 3` (`PROMOTION_MAX_ATTEMPTS`).
- `promoted_to` is unset (not already promoted).

These are module constants, not settings. The sweep selects candidates with Qdrant payload filters and checks the rest in Python.

## The write path

```
 1. select eligible concepts                      Qdrant scroll
 2. for each:
     a. render a markdown body via the LLM
     b. validate the rendering                    pydantic (PromotionRender)
     c. compute the vault path                    deterministic from namespace/topic/title
     d. resolve path conflicts                    read existing frontmatter
     e. create (or re-adopt) the curated point    CuratedPlane.create
     f. write the vault file                      VaultWriter: write-log first, then file
     g. transition the concept to 'promoted'      typed transition (promoted_to, promoted_at)
     h. emit an ops-alerts Thought
```

The curated point is created **before** the file is written, so an unrelated-lineage conflict fails closed before any vault path is overwritten. A retry after a failed file write is safe: `CuratedPlane.create` returns the existing row and the file is written with its `object_id`.

### Rendering

Prompt: `src/musubi/llm/prompts/promotion-render/v1.txt`, at temperature 0.2. The model returns JSON `{body, wikilinks, sections}`: a self-contained markdown body with at least one H2, no frontmatter, and no AI disclaimers. Concept fields and supporting memories are passed as untrusted data, not as instructions.

The rendering is separate from the concept's `content`: the concept is a machine summary, the curated note is prose for people.

**LLM endpoint:** the promotion client (`HttpxPromotionClient`) posts to `{OLLAMA_URL}/api/chat` with `LLM_MODEL`. It does **not** use the `LIFECYCLE_LLM_*` settings that maturation and synthesis use (ADR 0043).

### Validation

```python
class PromotionRender(BaseModel):
    body: str = Field(min_length=100, max_length=20000)
    wikilinks: list[str]
    sections: list[str]
```

The validator rejects a body with no H2, or one containing "as an AI model" / "as a language model". There is **no corrective-prompt retry**: a policy failure is recorded as a rejection (below), and the concept is tried again on a later sweep while it has attempts left.

### Vault path

```python
def compute_path(concept) -> str:
    topics = concept.topics or concept.linked_to_topics
    primary_topic = (slugify(topics[0]) or "_misc") if topics else "_misc"
    slug = slugify(concept.title)
    return f"curated/{namespace_to_dir(concept.namespace)}/{primary_topic}/{slug}.md"
```

`namespace_to_dir` keeps the first two segments (`tenant/presence`). `slugify` reduces to `[a-z0-9-]`, so a topic like `infrastructure/gpu` becomes `infrastructure-gpu`, and LLM-supplied topics cannot traverse paths. `VaultWriter.write_curated` also refuses any path that resolves outside the vault root.

Example: namespace `alex/shared/concept`, primary topic `infrastructure/gpu`, title "CUDA 13 driver 575":

```
curated/alex/shared/infrastructure-gpu/cuda-13-driver-575.md
```

### Path conflicts

If the target file already exists, its frontmatter is parsed:

- Corrupt frontmatter: deterministic failure, recorded as a rejection. The file is not overwritten.
- `musubi-managed: true` and `promoted_from` is this concept: rewrite in place, reusing the file's `object_id` (idempotent re-promotion).
- `musubi-managed: true` and a different concept: write a sibling `<slug>-v2.md` and emit an ops-alerts Thought.
- `musubi-managed: false` (human-authored): write a sibling `<slug>-promoted-<first 8 chars of concept id>.md` and emit an ops-alerts Thought.

### Write-log and file write

`VaultWriter` (`src/musubi/vault/writer.py`) records `(path, body_hash)` in the write-log **before** writing the file, so the vault watcher recognises Musubi's own write and does not re-ingest it. See [[06-ingestion/vault-sync]]. The write-log lives at `vault-writelog.db` next to the lifecycle database.

### Linkage

- Curated point: `promoted_from=<concept id>`, `promoted_at=now`, `state="matured"`, `musubi-managed: true` in the file.
- Concept: transitioned to `promoted` with `promoted_to=<curated id>` and `promoted_at`, via the lifecycle coordinator. If the transition is durably **pending**, the sweep reports the concept as not promoted this run and the reconciler finishes it.

### Operator notification

A Thought from `lifecycle-worker` on channel `ops-alerts`, `to_presence="all"`, in namespace `lifecycle-worker/ops/thought`, importance 5:

```
[Concept Promoted] Promoted concept '{title}' to curated/... Please review.
```

A failed notification is logged and does not undo the promotion.

## Rejection and failure

**Deterministic failures** (render policy validation, path computation, corrupt frontmatter at the target, curated model validation) call `ConceptPlane.record_promotion_rejection`:

- `promotion_attempts += 1`;
- `promotion_rejected_at = now`;
- `promotion_rejected_reason = "..."`.

The concept stays `matured`. After three attempts the gate stops selecting it until an operator intervenes.

**Transient failures** (LLM transport errors, malformed LLM envelopes, vault I/O, Qdrant, transition errors) do not touch the rejection fields or use up an attempt. They are logged with a traceback and retried on the next sweep.

## Operator actions

| Action | CLI | API |
|---|---|---|
| Force-promote to an existing curated row | `musubi promote force <id> --namespace <ns> --curated-id <cid>` | `POST /v1/concepts/{id}/promote?namespace=<ns>` `{promoted_to, reason}` |
| Record a rejection (one strike) | `musubi promote reject <id> --namespace <ns> --reason "..."` | `POST /v1/concepts/{id}/reject?namespace=<ns>` `{reason}` |
| Archive a curated row | (none) | `DELETE /v1/curated/{id}?namespace=<ns>` (transitions the point to `archived`; the vault file is not moved) |
| Move a concept to another state | (none) | `POST /v1/lifecycle/transition` `{object_id, to_state, actor, reason}` |

All of these require an **operator-scoped** token, except `DELETE /v1/curated`, which requires write access to the namespace. `musubi promote reject` records a rejection only; it does not demote the concept.

## Rollback

To undo a bad promotion:

1. Archive the curated row (`DELETE /v1/curated/{id}`).
2. Transition the concept with `POST /v1/lifecycle/transition`.
3. Remove or edit the vault file by hand if needed. The vault watcher or the 6-hourly `vault_reconcile` job picks up the change.

Every transition records a LifecycleEvent, so the audit trail keeps the full history.

## Test Contract

**Module under test:** `src/musubi/lifecycle/promotion.py`, `src/musubi/llm/promotion_client.py`, `src/musubi/vault/writer.py`

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
11. `test_rendering_retry_corrective_prompt` (skipped; no retry is implemented)

Path:

12. `test_path_derived_from_topic_and_title`
13. `test_path_conflict_with_same_concept_rewrites_in_place`
14. `test_path_conflict_with_other_concept_writes_sibling`
15. `test_path_conflict_with_human_file_writes_sibling_and_logs`
16. `test_path_sanitizes_topics_against_traversal`
17. `test_vault_writer_rejects_path_escape`

Write-log (skipped in this file; the write-log echo filter is covered by `tests/vault/test_sync.py`):

18. `test_writelog_entry_precedes_file_write`
19. `test_file_written_atomically`
20. `test_watcher_sees_writelog_and_skips_reindex`

Replay identity:

21. `test_idempotent_replay_reuses_existing_vault_object_id`
22. `test_idempotent_replay_reuses_vault_id_when_qdrant_also_exists`
23. `test_idempotent_replay_fails_closed_on_missing_vault_object_id`
24. `test_idempotent_replay_fails_closed_on_invalid_vault_object_id`
25. `test_idempotent_replay_adopts_persisted_qdrant_identity`
26. `test_idempotent_replay_fails_closed_on_unrelated_lineage`

Qdrant:

27. `test_curated_point_upserted_with_promoted_from`
28. `test_concept_state_set_to_promoted`
29. `test_bidirectional_links_set_in_single_batch`

Notification:

30. `test_lifecycle_events_emitted_for_both_sides`
31. `test_thought_emitted_to_ops_alerts`

Failure:

32. `test_promotion_rejected_after_3_attempts_stops_retrying`
33. `test_deterministic_rendering_failure_increments_attempts`
34. `test_transient_rendering_failure_leaves_attempts_unchanged`
35. `test_deterministic_post_render_failure_increments_attempts`
36. `test_transient_post_render_failure_leaves_attempts_unchanged`
37. `test_deterministic_model_validation_failure_increments_attempts`

Skipped (concurrency, CLI, property and integration):

38. `test_concurrent_promotion_of_different_concepts_ok`
39. `test_concurrent_promotion_of_same_concept_one_wins`
40. `test_cli_force_promote_with_custom_body`
41. `test_cli_reject_sets_rejected_fields_and_demotes`
42. `hypothesis: every successful promotion produces exactly one curated file and one Qdrant point`
43. `integration: happy path — 1 concept → 1 file in vault/, 1 point in musubi_curated, both linked, ops-alert present`
44. `integration: path conflict with human file — sibling created, no human file modified`
45. `integration: rollback flow — promote then archive`
