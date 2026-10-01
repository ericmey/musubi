---
title: Lifecycle
section: 04-data-model
tags: [data-model, lifecycle, section/data-model, state-machine, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: ["tests/lifecycle/__init__.py", "tests/lifecycle/test_lifecycle.py"]
---
# Lifecycle

The state machine for every memory object. Transitions are explicit, auditable, and enforced by a typed transition function.

## States

```python
# src/musubi/types/common.py:29-37

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

The legal transitions per object type are the `_ALLOWED` table in `src/musubi/types/lifecycle_event.py:32-62`. That table is the authority; the diagrams below are a reading of it.

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
  ├──(rewrite via supersession)──► superseded
  │
  └──(file deletion)──► archived

archived ──(restore)───► matured   (operator scope)
```

CuratedKnowledge has no `promoted` state. When a concept is promoted, the concept moves to `promoted`; a new CuratedKnowledge object is created in `matured` with `promoted_from: <concept-id>` and `promoted_at` set.

### SynthesizedConcept

```
synthesized ──(24h AND reinforcement_count ≥ 3 AND no contradictions)──► matured
                                          │
                                          ├──(promotion gate passes)──► promoted   (terminal)
                                          │
                                          ├──(decay rule)──► demoted
                                          │
                                          └──(supersession)──► superseded

demoted ──(reinstate)──► matured   (operator scope)
```

A concept with a non-empty `contradicts` list, or with fewer than 3 reinforcements, stays `synthesized` (`src/musubi/lifecycle/maturation.py:813-826`).

### SourceArtifact

Artifacts have two independent axes:

- **Lifecycle axis (`state`):** `matured → archived` or `matured → superseded`. Both targets are terminal.
- **Indexing axis (`artifact_state`, `src/musubi/types/common.py:39`):** `indexing → indexed`, `indexing → failed`, or `stored_unindexed` (bytes stored, never indexed). It is not governed by `transition()`.

An artifact is typically `state: matured` its whole life and moves only on the indexing axis. The `demotion_artifact` sweep archives old, unreferenced artifacts only when `MUSUBI_ARTIFACT_ARCHIVAL_ENABLED` is set.

### Thought

```
provisional ──► matured ──► archived
     └────────────────────► archived
```

## Transition function

Every state change goes through `src/musubi/lifecycle/transitions.py:163-176`:

```python
def transition(
    client: QdrantClient,
    *,
    coordinator: LifecycleTransitionCoordinator,
    object_id: KSUID,
    target_state: LifecycleState,
    actor: str,                         # presence or system id doing the transition
    reason: str,                        # short human-readable reason, required
    lineage_updates: LineageUpdates | None = None,
    correlation_id: str = "",
    sink: LifecycleEventSink | None = None,   # compatibility only; the coordinator persists events
    expected_version: int | None = None,      # optimistic fence
    namespace: str | None,                    # required; None means "unqualified lookup"
) -> Result[TransitionResult | TransitionPending, TransitionError]:
    ...
```

Behavior:
1. Locate the object across the plane collections. An id that resolves to more than one row is refused (`ambiguous_object_id`), never guessed.
2. If `expected_version` is given and does not match, return `version_fence_violation`.
3. Validate `(current_state, target_state)` against the allowed-transition table.
4. Apply lineage updates (`superseded_by`, `supersedes`, `merged_from`, `contradicts`, `promoted_to`, `promoted_at`) and reject supersession cycles.
5. Hand the intent to the coordinator, which applies the version-fenced mutation (`state`, `updated_at` / `updated_epoch`, `version + 1`) and persists the `LifecycleEvent`.
6. Return `Ok(TransitionResult)`, `Ok(TransitionPending)` when the coordinator defers, or `Err(TransitionError)`.

`TransitionError.code` is one of `not_found`, `illegal_transition`, `missing_reason`, `circular_supersession`, `invariant_violation`, `lifecycle_event_write_failed`, `version_fence_violation`, `ambiguous_object_id` (`transitions.py:112-160`). An invalid transition never mutates the payload.

## LifecycleEvent (audit log)

Every transition produces an event. The model is `LifecycleEvent` in `src/musubi/types/lifecycle_event.py:95-140`:

```python
class LifecycleEvent(BaseModel):
    event_id: KSUID
    object_id: KSUID                    # subject
    object_type: str                    # episodic | curated | concept | artifact | thought
    namespace: str
    schema_version: int
    from_state: LifecycleState
    to_state: LifecycleState
    actor: str                          # presence or system id
    reason: str
    occurred_at: datetime
    occurred_epoch: float
    lineage_changes: dict               # e.g., {"superseded_by": KSUID}
    correlation_id: str                 # request correlation ID; "" for background jobs
```

The model validator rejects an event whose `(from_state, to_state)` is illegal for its `object_type`. A sibling `CaptureEvent` (same file) records an object's initial creation.

Stored in:
- **sqlite** at `LIFECYCLE_SQLITE_PATH` (default `/var/lib/musubi/lifecycle/work.sqlite`, `.env.example:38`). This is the canonical store (`src/musubi/lifecycle/runner.py:575-576`).
- **Qdrant mirror** `musubi_lifecycle_events`: the collection and its indexes are declared in `src/musubi/store/specs.py`, but nothing writes to it yet (`src/musubi/lifecycle/events.py:13-14`). Planned, not implemented.

## Invariants

Enforced in pydantic `model_validator` or at transition time:

1. `state` must be in the allowed set for the object's type (the `state` `Literal` on each model, plus the transition table).
2. A concept in `promoted` requires `promoted_to` and `promoted_at` (`src/musubi/types/concept.py:65-66`). A curated object with `promoted_from` requires `promoted_at` (`src/musubi/types/curated.py:46-50`).
3. Every transition requires a non-empty `reason` (`missing_reason`).
4. `version ≥ 1`; each transition increments it.
5. `updated_epoch ≥ created_epoch`.
6. Circular supersession is rejected (A → B → A), and an object cannot supersede itself (`src/musubi/types/base.py:179-182`).

## Decay rules (Lifecycle Worker)

Scheduled jobs apply these. Times are UTC cron triggers (`src/musubi/lifecycle/maturation.py`, `src/musubi/lifecycle/demotion.py`); [[06-ingestion/lifecycle-engine]] has the full job registry.

### Episodic maturation (hourly, at :13)
- Select `state == "provisional"` AND `created_epoch < now - 1h`.
- For each: score importance via the LLM, normalize tags, transition to `matured`.

### Episodic provisional TTL (hourly, at :17)
- Select `state == "provisional"` AND `created_epoch < now - 7d`.
- Transition to `archived` (never matured; probably noise). Reason: `provisional-ttl`.

### Episodic demotion (weekly, Sunday 03:45)
- Select `state == "matured"` AND `access_count == 0` AND `reinforcement_count == 0` AND `updated_epoch < now - 60d` AND `importance < 4`.
- Transition to `demoted`. Reason: `decay-rule:untouched-low-importance`.

### Concept maturation (daily, 03:30)
- Select `state == "synthesized"` AND `created_epoch < now - 24h` AND `reinforcement_count ≥ 3` AND empty `contradicts`.
- Transition to `matured`.

### Concept demotion (daily, 05:00)
- Select `state == "matured"` AND `last_reinforced_epoch < now - 30d` (or, if never reinforced, `created_epoch < now - 30d`).
- Transition to `demoted`. Reason: `decay-rule:no-reinforcement`.

Thresholds: the maturation thresholds are fields of `MaturationConfig` (`src/musubi/lifecycle/maturation.py:297-316`), overridable at `build_maturation_jobs(config=...)`; the demotion thresholds are module constants (`src/musubi/lifecycle/demotion.py:30-34`). None of them is an environment setting yet.

## "No silent mutation" rule

It is an invariant of Musubi that **every state change produces a LifecycleEvent**. This means:

- Every Qdrant point update that changes `state` must go through `transition()` and its coordinator, which pairs it with an event row.
- The API's state-changing endpoints produce events.
- Background jobs produce events.
- `LifecycleEventSink.record()` commits each event to sqlite synchronously and returns `Ok` only after the commit; there is no batching or background flusher (`src/musubi/lifecycle/events.py:6-9`).

Writing `set_payload` on `state` directly, bypassing `transition()`, violates the rule.

## Test Contract

**Module under test:** `src/musubi/lifecycle/transitions.py`, `src/musubi/types/lifecycle_event.py` (tests in `tests/lifecycle/test_lifecycle.py`)

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
13. `test_concurrent_transitions_stale_expected_version_fence_violation` (a stale `expected_version` is refused, not last-writer-wins)
14. `test_sqlite_event_db_survives_worker_restart`  (events persist across crashes)

Bullets 7-11 are currently `skip`-marked in that file and covered by the per-sweep test modules (`tests/lifecycle/test_maturation.py`, `test_demotion.py`, `test_promotion.py`).

Property tests:

15. `test_hypothesis_state_machine_reachability` — every declared allowed transition is reachable from some state; no state is orphaned.
16. `test_hypothesis_monotone_invariants` — version, updated_epoch never decrease across any sequence of legal transitions.

## Why this much ceremony

At first glance, `transition()` + events look like overkill for a small-team memory system. It's not:

- Without an audit trail, "why does this memory say X?" becomes unanswerable after a week.
- Without typed transitions, silent `set_payload` calls break assumptions deep in retrieval (stale `state`, desynced versions).
- Without lineage, we can't rebuild trust in the system — e.g., "did this concept come from matured evidence, or did someone patch it?"

The ceremony pays for itself the first time you want to debug a weird retrieval result.
