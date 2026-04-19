---
title: "Slice: Synthesized concept plane"
slice_id: slice-plane-concept
section: _slices
type: slice
status: done
owner: vscode-cc-opus47
phase: "4 Planes"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-plane-episodic]]"]
blocks: ["[[_slices/slice-lifecycle-synthesis]]", "[[_slices/slice-lifecycle-promotion]]"]
---

# Slice: Synthesized concept plane

> Bridge layer. Clustered episodic reinforcement emerges as concept objects; candidates for promotion into curated.

**Phase:** 4 Planes · **Status:** `done` · **Owner:** `vscode-cc-opus47`

## Specs to implement

- [[04-data-model/synthesized-concept]]

## Owned paths (you MAY write here)

- `musubi/planes/concept/`
- `tests/planes/test_concept.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/episodic/`
- `musubi/planes/curated/`
- `musubi/lifecycle/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-plane-episodic]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-lifecycle-synthesis]]
- [[_slices/slice-lifecycle-promotion]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — vscode-cc-opus47 — claim

- Claimed slice atomically via `gh issue edit 21 --add-assignee @me`. Issue #21, PR #42 (draft).
- Branch `slice/slice-plane-concept` off `v2`.
- **Slice fix-up:** corrected `owns_paths` from `musubi/planes/synthesis/` → `musubi/planes/concept/` to match the canonical plane-name convention used by `src/musubi/types/concept.py`, `_PLANE_TO_COLLECTION["concept"]` in `src/musubi/store/names.py`, and the `musubi_concept` collection. The `synthesis/` name conflated this slice with `slice-lifecycle-synthesis` (which owns `src/musubi/lifecycle/synthesis.py`). Spec's `## Test Contract` "Module under test" line updated in the same PR with `spec-update:` trailer.

### 2026-04-19 — vscode-cc-opus47 — handoff to in-review

- Landed `src/musubi/planes/concept/{__init__,plane}.py`: `ConceptPlane` with the synthesized→matured→{promoted, demoted, superseded, archived} state machine, `merged_from`-min-3 + promoted/rejected mutual-exclusion write-side guards, `reinforce`/`mark_accessed` distinguishing new evidence from recall, and `transition`/`record_promotion_rejection` for the lifecycle-engine callers.
- Tests: 21 passing + 17 skipped-with-reason in `tests/planes/test_concept.py`. Coverage 94 % branch on `src/musubi/planes/concept/` (gate is 90 %). `make check` clean: ruff format + lint + mypy strict + pytest. `make tc-coverage SLICE=slice-plane-concept` exits 0.
- `make agent-check` passes for everything in this slice's diff. The only non-warning the tool reports is a `slice-lifecycle-engine` frontmatter↔Issue drift (#11, Cowork's slice in a parallel session) — unrelated to this PR.
- **Cross-slice ticket opened:** `_inbox/cross-slice/slice-plane-concept-slice-types-promotion-attempts.md` — the spec declares `promotion_attempts: int` and `last_reinforced_at: datetime | None` on `SynthesizedConcept`, but the type model lacks both. Plane ships without writing them; `slice-types` will need to add the fields before `slice-lifecycle-promotion` can land its retry-backoff predicate.
- PR #42 marked ready for review.

#### Test Contract coverage matrix

| # | Bullet | State | Where |
|---|---|---|---|
| 1 | `test_concept_requires_min_3_merged_from` | ✓ passing | `tests/planes/test_concept.py` |
| 2 | `test_concept_created_in_synthesized_state` | ✓ passing | `tests/planes/test_concept.py` |
| 3 | `test_concept_promoted_to_requires_state_promoted` | ✓ passing | `tests/planes/test_concept.py` |
| 4 | `test_concept_promotion_rejected_fields_mutually_exclusive_with_promoted_fields` | ✓ passing | `tests/planes/test_concept.py` |
| 5 | `test_synthesis_clusters_episodic_memories` | ⏭ skipped | deferred → slice-lifecycle-synthesis (`src/musubi/lifecycle/synthesis.py`) |
| 6 | `test_synthesis_creates_concept_from_cluster_of_3_plus` | ⏭ skipped | deferred → slice-lifecycle-synthesis |
| 7 | `test_synthesis_skips_clusters_below_3` | ⏭ skipped | deferred → slice-lifecycle-synthesis |
| 8 | `test_synthesis_matches_existing_concept_and_reinforces` | ⏭ skipped | deferred → slice-lifecycle-synthesis (plane ships `reinforce`, see bullet 16) |
| 9 | `test_synthesis_detects_contradiction_and_flags_both` | ⏭ skipped | deferred → slice-lifecycle-synthesis |
| 10 | `test_synthesis_idempotent_across_runs_on_same_input` | ⏭ skipped | deferred → slice-lifecycle-synthesis |
| 11 | `test_synthesis_respects_namespace_isolation` | ⏭ skipped | deferred → slice-lifecycle-synthesis (plane-level isolation covered) |
| 12 | `test_synthesis_handles_ollama_unavailable_by_skipping_gracefully` | ⏭ skipped | deferred → slice-lifecycle-synthesis |
| 13 | `test_concept_matures_after_24h_without_contradiction` | ⏭ skipped | deferred → slice-lifecycle-maturation (`src/musubi/lifecycle/maturation.py`) |
| 14 | `test_concept_matures_reset_if_contradiction_appears` | ⏭ skipped | deferred → slice-lifecycle-maturation |
| 15 | `test_concept_demotes_after_30d_no_reinforcement` | ⏭ skipped | deferred → slice-lifecycle-maturation |
| 16 | `test_reinforcement_count_increments_on_match` | ✓ passing | `tests/planes/test_concept.py` |
| 17 | `test_access_count_does_not_affect_reinforcement_count` | ✓ passing | `tests/planes/test_concept.py` |
| 18 | `test_promotion_gate_all_conditions_required` | ⏭ skipped | deferred → slice-lifecycle-promotion (`src/musubi/lifecycle/promotion.py`) |
| 19 | `test_promotion_writes_curated_file_and_links_back` | ⏭ skipped | deferred → slice-lifecycle-promotion |
| 20 | `test_promotion_sets_concept_state_promoted` | ✓ passing | `tests/planes/test_concept.py` |
| 21 | `test_promotion_rejected_sets_rejected_fields` | ⏭ skipped | deferred → slice-lifecycle-promotion (plane exposes `record_promotion_rejection`) |
| 22 | `test_promotion_retry_backoff_after_failure` | ⏭ skipped | deferred → slice-lifecycle-promotion (blocked by cross-slice ticket on `promotion_attempts`) |
| 23 | `test_contradicted_concept_blocked_from_promotion` | ⏭ skipped | deferred → slice-lifecycle-promotion |
| 24 | `test_promotion_produces_thought_notification_to_operator` | ⏭ skipped | deferred → slice-lifecycle-promotion + slice-plane-thoughts |
| 25 | `hypothesis: merged_from list is non-empty, all entries unique, all valid KSUIDs` | ⊘ out-of-scope | property test — deferred to a follow-up `test-property-concept` slice. The plane enforces non-empty (`min_length=3`) on create but relies on the type model's KSUID-format validation for entry validity; the bijection-style "all entries unique" claim is an emergent invariant of the synthesis worker (slice-lifecycle-synthesis), which dedupes sources before calling `reinforce`. |
| 26 | `hypothesis: state transitions are a subset of the declared allowed graph` | ⊘ out-of-scope | property test — the `LifecycleEvent` validator already enforces this for every transition (see `src/musubi/types/lifecycle_event.py::is_legal_transition`), so a dedicated property test would re-test the lifecycle-event slice's contract. Deferred to a future `test-property-lifecycle` slice that exercises the full transition graph across all object types. |

## Cross-slice tickets opened by this slice

- [`_inbox/cross-slice/slice-plane-concept-slice-types-promotion-attempts.md`](../_inbox/cross-slice/slice-plane-concept-slice-types-promotion-attempts.md) — open against `slice-types`. `SynthesizedConcept` is missing `promotion_attempts: int` and `last_reinforced_at: datetime | None` per spec; plane ships without writing them.

## PR links

- #42 — `feat(planes): slice-plane-concept` (in-review)
