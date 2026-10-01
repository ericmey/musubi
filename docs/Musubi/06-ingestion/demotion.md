---
title: Demotion
section: 06-ingestion
tags: [decay, demotion, ingestion, lifecycle, section/ingestion, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["src/musubi/lifecycle/demotion.py", "tests/lifecycle/test_demotion.py"]
---
# Demotion

The opposite of promotion. Matured memories and concepts that have not earned their place over time are demoted, not deleted. Demoted objects stay in the index for lineage and forensics but are filtered out of default retrieval.

Code: `src/musubi/lifecycle/demotion.py`.

## Why demote instead of delete

- **Forensics:** "Why does this concept say X?" can only be answered if the evidence (old memories) still exists.
- **Lineage:** supersession chains need the superseded object to exist.
- **Reversibility:** a demoted object can be brought back. A delete cannot.
- **Safety:** keeping too much is better than erasing a memory someone cared about.

Hard deletes need operator scope (for example `POST /v1/artifacts/{id}/purge`). See [[10-security/auth]] and [[09-operations/runbooks]].

## Rules

All schedules are UTC and run in the lifecycle worker ([[06-ingestion/lifecycle-engine]]). Each job takes its own lock (`demotion_episodic.lock`, `demotion_concept.lock`, `demotion_artifact.lock`).

### Episodic demotion (weekly)

`demotion_episodic`, **Sundays 03:45**.

```
state == "matured"
  AND access_count == 0
  AND reinforcement_count == 0
  AND updated_epoch < now - 60 days
  AND importance < 4
```

Rationale:

- Never accessed and never reinforced: no signal it was useful.
- Older than 60 days: synthesis and retrieval have had time to surface it.
- Low importance: captured as noise.

All conditions must hold. Any one failing protects the memory. Selected rows transition to `demoted` with reason `decay-rule:untouched-low-importance`.

### Concept demotion (daily)

`demotion_concept`, **daily 05:00**.

```
state == "matured"
  AND (last_reinforced_epoch < now - 30 days
       OR (last_reinforced_epoch is null AND created_epoch < now - 30 days))
```

A concept should keep being reinforced by new memories. Thirty days without reinforcement means the pattern is not recurring, or newer synthesis replaced it. Selected concepts transition to `demoted` with reason `decay-rule:no-reinforcement`, and the job emits an `ops-alerts` Thought for each.

### Artifact archival (weekly, opt-in)

`demotion_artifact`, **Sundays 04:15**, right after episodic demotion so the reference check sees fresh demotions. It is a no-op unless the deployment sets **`MUSUBI_ARTIFACT_ARCHIVAL_ENABLED=true`**. The switch is global, not per-namespace.

```
state == "matured"
  AND created_epoch < now - 180 days
  AND not referenced (supported_by) by any episodic, curated or concept row, in any state
```

Selected artifacts transition to `archived` with reason `decay-rule:unreferenced-expired`. The blob and chunks are kept, and the artifact drops out of default retrieval. There is no size threshold: `DEMOTION_ARTIFACT_MIN_SIZE` is defined but not applied. Reclaiming storage is a separate, explicit operator action: `POST /v1/artifacts/{id}/purge?namespace=<ns>` (operator scope) deletes the metadata and the blob.

## Reinstatement

There is no reinstatement command or endpoint. An operator can move a demoted object back with the generic transition endpoint:

```
POST /v1/lifecycle/transition            (operator scope)
{"object_id": "<ksuid>", "to_state": "matured", "actor": "admin", "reason": "used this yesterday"}
```

This records a LifecycleEvent. It does **not** reset `last_reinforced_at`, so a reinstated concept that is not reinforced may be demoted again by the next daily sweep. `demotion.reinstate()` resets the clock as well, but nothing in the API, CLI or worker calls it. (Not exposed.)

## Parameters

Module constants in `src/musubi/lifecycle/demotion.py`, not environment settings:

```python
DEMOTION_EPISODIC_AGE_DAYS = 60
DEMOTION_EPISODIC_MAX_IMPORTANCE = 4
DEMOTION_CONCEPT_NO_REINFORCE_DAYS = 30
DEMOTION_ARTIFACT_AGE_DAYS = 180
DEMOTION_ARTIFACT_MIN_SIZE = 1_000_000   # defined, not applied
```

The only runtime switch is `MUSUBI_ARTIFACT_ARCHIVAL_ENABLED`.

## Interaction with retrieval

Ranked retrieval (`fast`, `deep`, `blended`) defaults to `state IN ("matured", "promoted")`, so demoted, archived and superseded rows are hidden. In `fast` mode, `include_archived: true` adds `demoted`, `archived` and `superseded`. In `deep` and `blended` modes `include_archived` is ignored; pass `state_filter` explicitly.

## Interaction with scoring

When a demoted or archived row does surface, its provenance component is 0.1 (see [[05-retrieval/scoring-model]]), so it almost always ranks below active results.

## Anti-patterns to watch for

### "access_count is always 0"

If retrieval stops incrementing `access_count`, episodic demotion over-fires and demotes memories that are in use. Retrieval access accounting lives in `src/musubi/retrieve/accounting.py`.

### Demotion right after reinforcement

A concept reinforced 29 days ago is safe today and eligible tomorrow. That is deliberate: concepts must keep earning their place. Episodic rows are protected more strongly, because any reinforcement (`reinforcement_count > 0`) or any access exempts them.

### Avalanche demotion after a migration

A re-embedding migration (see [[11-migration/re-embedding]]) can disturb `updated_epoch`. There is **no pause flag**: `DEMOTION_PAUSED_UNTIL` is not implemented. Plan migrations so they do not rewrite `updated_epoch` on untouched rows, or stop the lifecycle worker until the migration has been checked.

## Test Contract

**Module under test:** `src/musubi/lifecycle/demotion.py`

Episodic:

1. `test_episodic_demotion_selects_by_all_four_criteria`
2. `test_episodic_demotion_skips_if_accessed`
3. `test_episodic_demotion_skips_if_reinforced`
4. `test_episodic_demotion_skips_if_high_importance`
5. `test_episodic_demotion_skips_if_young`
6. `test_episodic_demotion_transitions_and_emits_event`

Concept:

7. `test_concept_demotion_selects_by_last_reinforced`
8. `test_concept_demotion_selects_when_never_reinforced_and_stale`
9. `test_concept_demotion_emits_ops_thought`
10. `test_concept_reinforcement_resets_demotion_clock`

Artifact:

11. `test_artifact_archival_off_by_default`
12. `test_artifact_archival_respects_referenced_by`
13. `test_artifact_archival_transitions_to_archived_keeps_blob`

Reinstatement (of the unexposed `reinstate()` helper):

14. `test_reinstate_moves_back_to_matured`
15. `test_reinstate_resets_reinforced_clock`
16. `test_reinstate_emits_event` (skipped)

Skipped (pause flag not implemented; property and integration):

17. `test_demotion_paused_flag_honored`
18. `test_demotion_paused_expired_resumes`
19. `hypothesis: demotion is idempotent across runs with no change in criteria`
20. `hypothesis: no object that transitions to demoted was accessed within the selection window`
21. `integration: seed 1000 memories with varied properties, run weekly demotion, count transitions matches criteria`
22. `integration: reinstatement round-trip — demote → reinstate → appears in default retrieval`
