---
title: Synthesized Concept
section: 04-data-model
tags: [bridge, concept, data-model, schema, section/data-model, status/draft, type/spec]
type: spec
status: draft
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: ["src/musubi/store/specs.py", "src/musubi/types/base.py", "src/musubi/types/concept.py", "src/musubi/types/episodic.py", "src/musubi/types/lifecycle_event.py", "src/musubi/types/thought.py", "tests/planes/test_concept.py", "tests/types/"]
---
# Synthesized Concept

The bridge between episodic memory and curated knowledge. Machine-generated hypotheses about patterns emerging from reinforced episodic memories.

## Why a distinct type

A synthesized concept is **not** episodic (it has no single event time; it's an abstraction over many). It is **not** curated either (it hasn't been human-reviewed and isn't authoritative). Treating it as a third type lets the scorer weight it appropriately (between episodic and curated) and lets promotion be a first-class transition rather than a magic state flip.

See [[13-decisions/0002-planes-not-tiers]] for the full rationale.

## Pydantic model

The model is `SynthesizedConcept` in `src/musubi/types/concept.py:23-72`, extending `MemoryObject` (inherited fields: see [[04-data-model/object-hierarchy]]). Concept-specific:

| Field | Type | Default / rule |
|---|---|---|
| `state` | `synthesized \| matured \| promoted \| demoted \| superseded` | `synthesized` |
| `title` | `str`, non-empty | LLM-generated |
| `synthesis_rationale` | `str`, non-empty | LLM's explanation of why the sources cluster |
| `topics` | `list[str]` | `[]` |
| `promoted_to` / `promoted_at` | `KSUID` / `datetime` | `None`; `state=promoted` requires both |
| `promotion_rejected_at` / `promotion_rejected_reason` | `datetime` / `str` | `None`; a rejection time requires a reason |
| `promotion_attempts` | `int ≥ 0` | `0` |
| `last_reinforced_at` / `last_reinforced_epoch` | `datetime` / `float` | `None`; the epoch is derived |

`importance` defaults to 5 (inherited). `merged_from` (inherited) lists the source memory ids; the model allows an empty list, but `ConceptPlane.create` refuses fewer than 3 (`src/musubi/planes/concept/plane.py:168-172`). There is no `merged_from_planes` model field; only a payload index of that name is declared.

## Allowed lifecycle states

For concepts specifically (`src/musubi/types/lifecycle_event.py:45-51`):

```
synthesized → matured → promoted      (success path; terminal)
                      ↘
                       → demoted      (no reinforcement for 30 days)
                      ↘
                       → superseded   (a newer concept replaces it)

demoted → matured                     (operator reinstate)
```

Transitions:

- `synthesized` (on creation from synthesis job).
- `synthesized → matured` (≥ 24h after creation, `reinforcement_count ≥ 3`, and no `contradicts` entries).
- `matured → promoted` (promotion gate passes; CuratedKnowledge written).
- `matured → demoted` (decay rule; no reinforcement in 30 days).
- `matured → superseded` (a newer concept merges in and replaces).
- `demoted → matured` (operator reinstate).

Concepts have no `archived` state.

## Qdrant layout

Collection: `musubi_concept`.

Indexes: the universal set plus `promoted_to`, `promotion_attempts`, `merged_from`, `merged_from_planes`, `contradicts`, `last_reinforced_epoch` (`src/musubi/store/specs.py:187-194`). `merged_from_planes` is declared but no writer populates it.

## Synthesis input selection

The synthesis job (see [[06-ingestion/concept-synthesis]], `src/musubi/lifecycle/synthesis.py`) produces concepts from *clusters of matured episodic memories*. Input selection:

1. Pull matured episodic memories newer than a per-identity-family cursor (stored in the lifecycle sqlite), plus a pool of carried-over candidates.
2. Cluster by dense similarity at `cluster_threshold = 0.70` (`SynthesisConfig`, `synthesis.py:425-445`), minimum cluster size 3.
3. For each cluster of ≥ 3 memories: ask the LLM for a title, content and rationale.
4. Check against existing concepts (dense similarity ≥ `match_threshold = 0.85`). If matched: *reinforce* the existing concept instead of creating a new one.

This is the **Mem0 extract-consolidate pattern** applied at the concept layer.

## Promotion gate

A concept is eligible for promotion when all are true:

- `reinforcement_count ≥ 3`
- `importance ≥ 6`
- `created_at` more than 48 hours ago (let contradictions surface)
- No `contradicts` entries at all
- `state` is `matured` (not synthesized; not demoted)
- `promotion_attempts < 3` (avoids infinite retry on broken concepts)
- Not already promoted (`promoted_to` is unset)

The gate is `_is_eligible` in `src/musubi/lifecycle/promotion.py:162-182`. Its thresholds are module constants (`PROMOTION_REINFORCEMENT_THRESHOLD = 3`, `PROMOTION_IMPORTANCE_THRESHOLD = 6`, `PROMOTION_MAX_ATTEMPTS = 3`, `promotion.py:34-36`); the 48-hour age is inline. They are global, not settings.

## Reinforcement

A concept is reinforced when:

1. **The synthesis job finds a new cluster that semantically matches it** (dense ≥ 0.85 to the concept vector). This bumps `reinforcement_count` and `last_reinforced_at`.
2. **Reads do not reinforce.** Retrieval may bump `access_count`; it never touches `reinforcement_count`.

Distinguishing these matters: we want promotion to be driven by *new evidence*, not just re-reads of existing evidence.

## Contradiction detection

During synthesis, when a candidate concept overlaps semantically with an existing one (≥ 0.75 but < 0.85), the LLM is asked: "Are these consistent, or do they contradict?" If contradicting, both concepts get `contradicts` entries pointing at each other (`synthesis.py:808-820`). Both are then blocked from maturation and promotion.

Flagged pairs are listed by `GET /v1/contradictions` (`src/musubi/api/routers/contradictions.py`). There is no resolve command or endpoint; resolving a contradiction is planned, not implemented.

## Test Contract

**Module under test:** `src/musubi/planes/concept/` (this slice — plane CRUD + transitions), `src/musubi/lifecycle/synthesis.py` (slice-lifecycle-synthesis), `src/musubi/lifecycle/promotion.py` (slice-lifecycle-promotion), `src/musubi/lifecycle/maturation.py` (slice-lifecycle-maturation)

Schema / basics:

1. `test_concept_requires_min_3_merged_from`
2. `test_concept_created_in_synthesized_state`
3. `test_concept_promoted_to_requires_state_promoted`
4. `test_concept_promotion_rejected_fields_mutually_exclusive_with_promoted_fields`

Synthesis (with `mock_ollama`):

5. `test_synthesis_clusters_episodic_memories`
6. `test_synthesis_creates_concept_from_cluster_of_3_plus`
7. `test_synthesis_skips_clusters_below_3`
8. `test_synthesis_matches_existing_concept_and_reinforces`
9. `test_synthesis_detects_contradiction_and_flags_both`
10. `test_synthesis_idempotent_across_runs_on_same_input`
11. `test_synthesis_respects_namespace_isolation`
12. `test_synthesis_handles_ollama_unavailable_by_skipping_gracefully`

Lifecycle:

13. `test_concept_matures_after_24h_with_3_reinforcements_without_contradiction`
14. `test_concept_matures_reset_if_contradiction_appears`
15. `test_concept_demotes_after_30d_no_reinforcement`
16. `test_reinforcement_count_increments_on_match`
17. `test_access_count_does_not_affect_reinforcement_count`

Promotion:

18. `test_promotion_gate_all_conditions_required`
19. `test_promotion_writes_curated_file_and_links_back`
20. `test_promotion_sets_concept_state_promoted`
21. `test_promotion_rejected_sets_rejected_fields`
22. `test_promotion_retry_backoff_after_failure`
23. `test_contradicted_concept_blocked_from_promotion`
24. `test_promotion_produces_thought_notification_to_operator`

Property / invariants:

25. `hypothesis: merged_from list is non-empty, all entries unique, all valid KSUIDs`
26. `hypothesis: state transitions are a subset of the declared allowed graph`

## Prior art

- Mem0 ADD/UPDATE/DELETE/NOOP: [https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413) — our synthesis mirrors the ADD/UPDATE paths; DELETE is our demotion; NOOP is the "below threshold" skip.
- Generative Agents "reflection" pass: [https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442) — we differ by separating reflection (daily summary; see [[06-ingestion/reflection]]) from synthesis (concept creation).

## Open questions

- Should we let humans create concepts directly? Current answer: **no.** If a human wants to state a concept, they write a CuratedKnowledge file. Concepts are machine-generated.
- Should concepts be visible to ordinary queries? Current answer: yes, but with a provenance weight lower than curated. See [[05-retrieval/scoring-model]].
