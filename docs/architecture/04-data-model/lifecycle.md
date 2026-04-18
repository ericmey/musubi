---
title: Lifecycle
section: 04-data-model
tags: [data-model, lifecycle, section/data-model, state-machine, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
---
# Lifecycle

The state machine for every memory object. Transitions are explicit, auditable, and enforced by a typed transition function.

## States

```python
# musubi/types/common.py

LifecycleState = Literal[
    "provisional",   # just captured; not eligible for deep retrieval
    "matured",       # passed maturation; standard queryable state
    "promoted",      # concepts: promoted to curated (terminal success)
    "synthesized",   # concepts only: fresh from synthesis job
    "demoted",       # removed from default retrieval; kept for provenance
    "archived",      # cold; not queryable in normal paths
    "superseded",    # replaced by a newer version; lineage link set
]
```

## Allowed transitions per type

### EpisodicMemory

```
provisional ──(maturation sweep)──► matured
     │                                 │
     │                                 ├──(demotion rule)──► demoted
     │                                 │
     │                                 ├──(merge into concept)──► unchanged  # merged_from only
     │                                 │
     │                                 └──(supersession write)──► superseded
     │
     └──(ttl expire 7d)──► archived  (if never matured)

demoted ──(reinstate)──► matured   (operator scope)
archived ──(restore)───► matured   (operator scope)
```

### CuratedKnowledge

```
matured  (starting state — no provisional for curated)
  │
  ├──(promotion; ARE we a promoted one?)──► matured            # flag promoted_from; state unchanged
  │
  ├──(rewrite via supersession)──► superseded
  │
  └──(file deletion)──► archived
```

Note: CuratedKnowledge doesn't have `promoted` as a state — `promoted` is a concept's state. When a concept is promoted, it stays in `promoted` state; a CuratedKnowledge object is created in `matured` state with `promoted_from: <concept-id>`.

### SynthesizedConcept

```
synthesized ──(24h, no contradiction)──► matured
   │                                       │
   │                                       ├──(promotion gate passes)──► promoted
   │                                       │
   │                                       ├──(decay rule)──► demoted
   │                                       │
   │                                       └──(supersession)──► superseded
   │
   └──(contradiction flagged)──► synthesized  # blocked from maturing until resolved
```

### SourceArtifact

```
indexing ──► indexed   (happy path)
indexing ──► failed    (chunking or embedding error)
indexed ──(explicit)──► archived
```

Artifacts also use `state: matured` throughout their useful life (separate from `artifact_state`). This is a second-axis state: `state` is the lifecycle axis; `artifact_state` is the indexing axis.

## Transition function

Every state change goes through `musubi/lifecycle/transitions.py`:

```python
def transition(
    client: QdrantClient,
    *,
    object_id: KSUID,
    target_state: LifecycleState,
    actor: str,                         # presence doing the transition
    reason: str,                        # short human-readable reason
    lineage_updates: LineageUpdates = None,  # optional: set supersedes, merged_from, etc.
) -> Result[TransitionResult, TransitionError]:
    ...
```

Behavior:
1. Fetch current object.
2. Validate `(current_state, target_state)` is an allowed transition for the object's type.
3. Apply transition: update `state`, bump `updated_at` / `updated_epoch`, `version++`.
4. Apply lineage updates (supersession links, merge-in sources, etc.).
5. Emit a `LifecycleEvent` audit row (see below).
6. Return `Ok(TransitionResult)`.

Invalid transitions return `Err(InvalidTransitionError(from, to, allowed))`.

## LifecycleEvent (audit log)

Every transition produces an event:

```python
class LifecycleEvent(BaseModel):
    event_id: KSUID
    object_id: KSUID                    # subject
    namespace: str
    from_state: LifecycleState
    to_state: LifecycleState
    actor: str                          # presence
    reason: str
    occurred_at: datetime
    occurred_epoch: float
    lineage_changes: dict               # e.g., {"supersedes_added": [KSUID]}
    correlation_id: str                 # request correlation ID
```

Stored in:
- **sqlite** at `/srv/musubi/lifecycle-state/events.db` (local, canonical).
- **Qdrant mirror** `musubi_lifecycle_events` (optional; for semantic search across the audit log — useful in reflection).

## Invariants

Enforced in pydantic `model_validator` or at transition time:

1. `state` must be in the allowed set for the object's type.
2. `state == "promoted"` requires `promoted_at` and `promoted_to` set (for concepts) or `promoted_from` (for the resulting curated).
3. `state == "superseded"` requires `superseded_by` to reference a live, same-type object in the same namespace.
4. `state == "demoted"` must have a `demoted_reason` in the lineage event.
5. `version` never decreases.
6. `updated_epoch` never decreases.
7. Circular supersession is rejected (A → B → A).
8. `merged_from` for a non-concept object is allowed but rare; logged and flagged in audit.

## Decay rules (Lifecycle Worker)

Scheduled jobs apply these:

### Episodic maturation (hourly)
- Select `state == "provisional"` AND `created_epoch < now - 1h`.
- For each: score importance via Ollama, normalize tags, transition to `matured`.

### Episodic demotion (weekly)
- Select `state == "matured"` AND `access_count == 0` AND `reinforcement_count == 0` AND `updated_epoch < now - 60d` AND `importance < 4`.
- Transition to `demoted`. Reason: `decay-rule:untouched-low-importance`.

### Episodic provisional TTL (hourly)
- Select `state == "provisional"` AND `created_epoch < now - 7d`.
- Transition to `archived` (never matured; probably noise).

### Concept maturation (daily)
- Select `state == "synthesized"` AND `created_epoch < now - 24h` AND no active contradictions.
- Transition to `matured`.

### Concept demotion (daily)
- Select `state == "matured"` AND `last_reinforced_at < now - 30d`.
- Transition to `demoted`. Reason: `decay-rule:no-reinforcement`.

All rules have hand-tunable thresholds in `config.py` with sensible defaults.

## "No silent mutation" rule

It is an invariant of Musubi that **every state change produces a LifecycleEvent**. This means:

- Every Qdrant point update that changes `state` or `version` must be paired with an event row.
- The API's PATCH endpoints produce events.
- Background jobs produce events.
- Events are batched to sqlite; flushed at most every 5s or every 100 events.

If a coder writes `set_payload` directly bypassing `transition()`, they've violated the rule. There's a lint rule + an integration test that scrolls recent Qdrant updates and checks for matching events.

## Test contract

**Module under test:** `musubi/lifecycle/transitions.py`, `musubi/lifecycle/states.py`

1. `test_valid_transition_succeeds_and_emits_event`
2. `test_invalid_transition_returns_typed_error`
3. `test_transition_bumps_version_and_updated_epoch`
4. `test_transition_preserves_lineage_through_supersession`
5. `test_circular_supersession_rejected`
6. `test_demotion_requires_reason`
7. `test_episodic_maturation_happy_path` (integration with mock_ollama)
8. `test_episodic_demotion_rule_selects_correctly` (property-ish)
9. `test_episodic_provisional_ttl_archives_not_deletes`
10. `test_concept_maturation_blocked_by_contradiction`
11. `test_concept_promotion_sets_all_required_fields`
12. `test_event_written_for_every_transition`
13. `test_concurrent_transitions_last_writer_wins_with_logged_warning`  (we accept last-write-wins for v1; conflicts are rare and surface in audit)
14. `test_event_batch_flushed_within_5s_under_load`
15. `test_sqlite_event_db_survives_worker_restart`  (events persist across crashes)

Property tests:

16. `hypothesis: state-machine reachability — every declared allowed transition is reachable from some state; no state is orphaned`
17. `hypothesis: monotone invariants — version, updated_epoch never decrease across any sequence of legal transitions`

## Why this much ceremony

At first glance, `transition()` + events look like overkill for a household memory system. It's not:

- Without an audit trail, "why does this memory say X?" becomes unanswerable after a week.
- Without typed transitions, silent `set_payload` calls break assumptions deep in retrieval (stale `state`, desynced versions).
- Without lineage, we can't rebuild trust in the system — e.g., "did this concept come from matured evidence, or did someone patch it?"

The ceremony pays for itself the first time you want to debug a weird retrieval result.
