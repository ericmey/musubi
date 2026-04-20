---
title: Demotion
section: 06-ingestion
tags: [decay, demotion, ingestion, lifecycle, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["src/musubi/lifecycle/demotion.py", "src/musubi/lifecycle/promotion.py", "tests/lifecycle/test_demotion.py", "tests/lifecycle/test_promotion.py"]
---
# Demotion

The opposite of promotion. Matured memories and concepts that haven't earned their place over time are demoted (not deleted). Demoted objects stay in the index for lineage and forensics but are filtered out of default retrieval.

## Why demote instead of delete

- **Forensics**: "Why does this concept say X?" can only be answered if the evidence (old memories) is still around.
- **Lineage**: Supersession chains require the superseded object to exist.
- **Reversibility**: A human can reinstate a demoted memory. Deletes are permanent.
- **Safety**: We'd rather keep too much than accidentally erase a memory the user cared about.

Hard deletes require operator scope and a reason. See [[10-security/auth]] for operator scope; the destructive-path docs are in [[09-operations/runbooks]].

## Rules

### Episodic demotion (weekly)

Runs Sunday 03:00, `demotion_episodic`.

Select:

```
state == "matured"
  AND access_count == 0
  AND reinforcement_count == 0
  AND updated_epoch < now - 60 days
  AND importance < 4
```

Rationale:

- Never accessed, never reinforced — no signal it was useful.
- Older than 60 days — enough time for synthesis/retrieval to have surfaced it if relevant.
- Low importance — captured as noise.

Transition to `demoted`. Reason: `decay-rule:untouched-low-importance`.

### Concept demotion (daily)

Runs 05:00, `demotion_concept`.

Select:

```
state == "matured"
  AND last_reinforced_at < now - 30 days
```

Rationale: a concept should keep getting reinforced by new memories. 30 days without reinforcement means either the pattern isn't recurring, or new synthesis found a replacement concept.

Transition to `demoted`. Reason: `decay-rule:no-reinforcement`.

Emit a Thought on `ops-alerts` ("Concept X demoted; reinforcement tapered off").

### Artifact archival (monthly, opt-in)

Artifacts are rarely demoted — they're raw evidence, cheap to keep. But for very large artifacts not cited in any curated or concept for 180 days:

```
state == "matured"
  AND NOT referenced_by_any_curated_or_concept
  AND size_bytes > 1_000_000
  AND created_epoch < now - 180 days
```

Transition `state=archived`. Blob stays; chunks stay; excluded from default retrieval. Operator can purge blob via `musubi-cli artifacts purge --hard <id>`.

Off by default; opt-in per-namespace.

## Reinstatement

Any demoted object can be reinstated:

```bash
musubi-cli reinstate <object-id> --reason "used this yesterday"
```

- Transition `demoted → matured`.
- Reset `last_reinforced_at = now`.
- Emit LifecycleEvent.

## Parameters (tunable)

```python
# config.py
DEMOTION_EPISODIC_AGE_DAYS = 60
DEMOTION_EPISODIC_MAX_IMPORTANCE = 4
DEMOTION_CONCEPT_NO_REINFORCE_DAYS = 30
DEMOTION_ARTIFACT_AGE_DAYS = 180
DEMOTION_ARTIFACT_MIN_SIZE = 1_000_000
```

Every threshold is a config key. We expect tuning from evals data over time.

## Interaction with retrieval

Default retrieval filter:

```
state IN ("matured", "promoted")
```

So demoted, archived, superseded are all hidden. Callers can opt in via `include_archived=True` and `include_superseded=True` on `RetrievalQuery`.

## Interaction with scoring

If a demoted object does surface (via an `include_archived=True` query), its `provenance` component drops to 0.1 (see [[05-retrieval/scoring-model#provenance]]). It'll almost certainly rank below any non-demoted result.

## Anti-patterns to watch for

### The "access count always 0" bug

If the access_count field isn't correctly incremented by retrieval, demotion will over-fire (demote memories that are actually being used). Test: `test_memory_recall_increments_access_count` in retrieval tests.

### The "demotion hides recent reinforcement" bug

If a memory was reinforced 29 days ago, it's safe today but will be demoted tomorrow. Seems harsh. The `reinforcement_count > 0` rule mitigates this — once reinforced even once, the memory is protected for far longer (until the combined age + no-access criteria).

Episodic demotion rule is:

```
access_count == 0 AND reinforcement_count == 0 AND age > 60d AND importance < 4
```

All four must hold. Any one being false protects the memory.

### Avalanche demotion after a migration

When we re-embed (see [[11-migration/re-embedding]]), `last_reinforced_at` and `updated_epoch` might not reflect real reinforcement. We pause demotion for 14 days after any re-embed migration via a flag `DEMOTION_PAUSED_UNTIL`.

## Test Contract

**Module under test:** `musubi/lifecycle/demotion.py`

Episodic:

1. `test_episodic_demotion_selects_by_all_four_criteria`
2. `test_episodic_demotion_skips_if_accessed`
3. `test_episodic_demotion_skips_if_reinforced`
4. `test_episodic_demotion_skips_if_high_importance`
5. `test_episodic_demotion_skips_if_young`
6. `test_episodic_demotion_transitions_and_emits_event`

Concept:

7. `test_concept_demotion_selects_by_last_reinforced`
8. `test_concept_demotion_emits_ops_thought`
9. `test_concept_reinforcement_resets_demotion_clock`

Artifact:

10. `test_artifact_archival_off_by_default`
11. `test_artifact_archival_respects_referenced_by`
12. `test_artifact_archival_transitions_to_archived_keeps_blob`

Reinstatement:

13. `test_reinstate_moves_back_to_matured`
14. `test_reinstate_resets_reinforced_clock`
15. `test_reinstate_emits_event`

Filter:

16. `test_default_retrieval_excludes_demoted`
17. `test_include_archived_includes_demoted`

Migration safety:

18. `test_demotion_paused_flag_honored`
19. `test_demotion_paused_expired_resumes`

Property:

20. `hypothesis: demotion is idempotent across runs with no change in criteria`
21. `hypothesis: no object that transitions to demoted was accessed within the selection window`

Integration:

22. `integration: seed 1000 memories with varied properties, run weekly demotion, count transitions matches criteria`
23. `integration: reinstatement round-trip — demote → reinstate → appears in default retrieval`
