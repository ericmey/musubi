---
title: "Slice: Types followup — add 6 missing fields cited by cross-slice tickets"
slice_id: slice-types-followup
section: _slices
type: slice
status: ready
owner: unassigned
phase: "1 Schema"
tags: [section/slices, status/ready, type/slice, types, cross-slice-consolidation]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]"]
blocks: []
---

# Slice: Types followup — add 6 missing fields

> Consolidates 6 open cross-slice tickets that all request small field additions to `src/musubi/types/`. Each was filed by a downstream slice that couldn't land its own test bullets because the types surface was missing a field the spec requires. Parent `slice-types` is `status: done` — this is the standard Option-3 followup pattern (same as blended-followup, episodic-followup).

**Phase:** 1 Schema · **Status:** `ready` · **Owner:** `unassigned`

## Why this slice exists

During the hidden-pile audit on 2026-04-19, six cross-slice tickets in `_inbox/cross-slice/` were identified as all targeting `src/musubi/types/`. Each sits as a skipped Test Contract bullet in a done slice, pointing here as the blocker. Consolidating into one slice avoids six micro-PRs and keeps the types surface coherent.

## Specs to implement

- [[04-data-model/episodic-memory]] (add `importance_last_scored_at`, reconcile `topics` vs `linked_to_topics`)
- [[04-data-model/synthesized-concept]] (add `topics`, `promotion_attempts`, `last_reinforced_at`)
- [[04-data-model/thoughts]] (add `in_reply_to`, `supersedes`)
- [[04-data-model/lifecycle]] (add `CaptureEvent` — or relax `LifecycleEvent` — for capture-time provenance)

## Owned paths (you MAY write here)

- `src/musubi/types/episodic.py`           (parent done — add `importance_last_scored_at`; add `topics` OR remove from spec per Option B)
- `src/musubi/types/concept.py`            (parent done — add `topics`, `promotion_attempts`, `last_reinforced_at`)
- `src/musubi/types/thought.py`            (parent done — add `in_reply_to`, `supersedes`)
- `src/musubi/types/lifecycle_event.py`    (parent done — Option A: add `CaptureEvent`, OR Option B: relax validator)
- `src/musubi/types/base.py`               (parent done — if any base-class change required)
- `src/musubi/store/specs.py`              (parent done — add `importance_last_scored_epoch` index)
- `tests/types/`                           (new tests + amendments)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/`          (downstream consumers; their followups unskip after this lands)
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/api/`
- `src/musubi/sdk/`
- `src/musubi/adapters/`
- `openapi.yaml`
- `proto/`

## Depends on

- [[_slices/slice-types]]   (done — this is the followup)

Start this slice only after every upstream slice has `status: done`. ✓ met.

## Unblocks

- **`slice-ingestion-capture` followup** — unskips bullet 5 (capture-lifecycle-event) once `CaptureEvent` (or relaxed validator) lands.
- **`slice-lifecycle-maturation` followup** — unskips bullet 24 (re-enrichment sweep) once `importance_last_scored_at` lands. Also reconciles topics-vs-linked_to_topics.
- **`slice-lifecycle-promotion` followup** — uses `concept.topics` for path derivation.
- **`slice-plane-concept` followup** — populates `last_reinforced_at` + `promotion_attempts` in reinforce/record-rejection paths.
- **`slice-plane-thoughts` followup** — implements `in_reply_to` chain query + supersedes semantics.

After this slice lands, five downstream follow-up PRs become cheap one-liners in the consumer slices.

## Fields to add

### 1. `EpisodicMemory.importance_last_scored_at: datetime | None = None`

**Source ticket:** [[_inbox/cross-slice/slice-lifecycle-maturation-slice-types-importance-last-scored-at]]

Purpose: supports the spec's re-enrichment sweep (`06-ingestion/maturation.md` §Re-enrichment on next sweep). `WHERE state='matured' AND (importance_last_scored_at IS NULL OR importance_last_scored_at < now-7d)`.

Implementation:
- Field declaration in `src/musubi/types/episodic.py` with `ensure_utc` validator.
- Qdrant index mirror in `src/musubi/store/specs.py::_EPISODIC_DELTAS`: `IndexSpec(field_name="importance_last_scored_epoch", schema="float")`.

### 2. `EpisodicMemory.topics` — OR spec amendment

**Source ticket:** [[_inbox/cross-slice/slice-lifecycle-maturation-slice-types-topics-vs-linked-to-topics]]

Purpose: spec (`06-ingestion/maturation.md` §Step 4) writes inferred topics as `topics`; model only has `linked_to_topics`.

**Pick one**:
- **Option A (recommended):** add `topics: list[str] = Field(default_factory=list)` to `EpisodicMemory`; matches `CuratedKnowledge.topics` semantics; keeps the `UNIVERSAL_INDEXES.topics` keyword index meaningful across collections.
- **Option B:** update spec to say `linked_to_topics`; smaller change; requires `spec-update:` trailer to `06-ingestion/maturation.md` + removing `topics` from `UNIVERSAL_INDEXES` for episodic.

Document the choice in the feat-commit message.

### 3. `SynthesizedConcept.topics: list[str] = Field(default_factory=list)`

**Source ticket:** [[_inbox/cross-slice/slice-lifecycle-promotion-slice-types-concept-topics]]

Purpose: `06-ingestion/promotion.md`'s `compute_path` uses `concept.topics[0]` to derive the markdown-file directory. Model currently has only `linked_to_topics`; promotion slice uses `linked_to_topics` as a fallback.

### 4. `SynthesizedConcept.promotion_attempts: int = Field(default=0, ge=0)`

**Source ticket:** [[_inbox/cross-slice/slice-plane-concept-slice-types-promotion-attempts]]

Purpose: supports promotion retry-backoff (`06-ingestion/promotion.md` §Promotion gate — `promotion_attempts < 3`). Index already exists in `store/specs.py`.

### 5. `SynthesizedConcept.last_reinforced_at: datetime | None = None`

**Source ticket:** (same as #4)

Purpose: maturation's 30-day reinforcement-staleness demotion timer. Index already exists (`_CONCEPT_DELTAS._last_reinforced_epoch`).

### 6. `Thought.in_reply_to: KSUID | None = None` + `Thought.supersedes: list[KSUID] = Field(default_factory=list)`

**Source ticket:** [[_inbox/cross-slice/slice-plane-thoughts-slice-types-missing-lineage-fields]] (redundant question in `_inbox/questions/` was deleted during tonight's audit)

Purpose: spec (`04-data-model/thoughts.md`) declares lineage fields on Thought that the model doesn't have. Blocks `test_thought_in_reply_to_chain_queries_correctly`.

Note: `Thought` inherits from `MusubiObject`, not `MemoryObject`. Confirm via `MemoryObject` superclass whether the fields belong there (affects other memory types) or on `Thought` directly (cleaner).

### 7. Lifecycle event for capture-time provenance

**Source ticket:** [[_inbox/cross-slice/slice-ingestion-capture-slice-types-capture-event-record]]

Purpose: `06-ingestion/capture.md` §Step 6 wants `LifecycleEvent(provisional → created)` at ingest, but validator rejects `provisional → provisional`.

**Pick one**:
- **Option A (recommended):** Add a new `CaptureEvent` type parallel to `LifecycleEvent`. Stored in the same sqlite ledger via additional table or reuse the table with a discriminator column. Clean separation of creation events vs state transitions.
- **Option B:** Relax `LifecycleEvent` validator to accept `from_state == to_state` when `from_state == "provisional"`, OR add an explicit `event_kind="creation"` discriminator. Smaller change; bends the "every event is a transition" invariant.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] All 6 fields (or their consolidated replacements per Options A/B) land in `src/musubi/types/`.
- [ ] Pydantic validation: `ensure_utc` applied on every new `datetime | None` field. No timezone-naive datetimes accepted.
- [ ] Qdrant indexes in `store/specs.py` updated where required (only `importance_last_scored_epoch` needs adding; the other indexed fields already exist).
- [ ] Branch coverage ≥ 85% on `src/musubi/types/` (matches parent slice-types' floor).
- [ ] Six cross-slice tickets in `_inbox/cross-slice/` updated to `status: resolved` with a one-line note pointing at the PR.
- [ ] `_inbox/questions/slice-plane-thoughts-missing-fields.md` deleted (resolved).
- [ ] `make tc-coverage` on affected slices shows no new `skipped` bullets; existing deferred bullets remain skipped pointing at their consumer-side followup slices (consumer unskips, not this slice).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Issue label `status:ready → status:in-progress` at claim time (Dual-update rule; drift-check is ✗).
- [ ] Spec `status:` updated via `spec-update:` trailer if prose changed (e.g., Option B on topics).

## Test Contract

Per-field validation + round-trip + index-presence tests:

1. `test_episodic_importance_last_scored_at_accepts_utc_datetime`
2. `test_episodic_importance_last_scored_at_rejects_naive_datetime`
3. `test_episodic_topics_field_exists` (if Option A chosen)
4. `test_concept_topics_field_accepts_list`
5. `test_concept_promotion_attempts_default_zero`
6. `test_concept_promotion_attempts_rejects_negative`
7. `test_concept_last_reinforced_at_accepts_utc_datetime`
8. `test_thought_in_reply_to_accepts_ksuid`
9. `test_thought_supersedes_accepts_ksuid_list`
10. `test_capture_event_validates` (Option A) OR `test_lifecycle_event_accepts_provisional_to_provisional_at_capture` (Option B)
11. `test_episodic_importance_last_scored_epoch_index_declared`
12. `test_all_fields_round_trip_through_model_dump_model_validate`

Explicitly out-of-scope (consumer-side unskips, NOT this slice):
- Unskipping `slice-lifecycle-maturation`'s bullet 24.
- Unskipping `slice-lifecycle-promotion`'s bullet 12.
- Unskipping `slice-plane-concept`'s bullets 15, 16, 22.
- Unskipping `slice-plane-thoughts`'s `test_thought_in_reply_to_chain_queries_correctly`.
- Unskipping `slice-ingestion-capture`'s bullet 5.

Those are downstream followup PRs against the respective consumer slices after THIS slice lands.

## Work log

### 2026-04-19 — operator — slice carved

- Consolidates 6 cross-slice tickets (+ 1 redundant question) into one followup slice. Parent `slice-types` is `status: done`; this is the Option-3 followup pattern established tonight.
- Recommended Option A on each either/or choice (keep invariants clean; prefer new fields over bending existing validators). Implementing agent has authority to choose B on either with justification in the feat-commit message.
- Paths and wikilinks verified against actual file layout 2026-04-19.

## Cross-slice tickets opened by this slice

- _(none yet; resolution path for the 6 source tickets is documented in DoD)_

## PR links

- _(none yet)_
