---
title: "Slice: H5 — unify all lifecycle state mutation behind LifecycleTransitionCoordinator"
slice_id: slice-h5-unify-state-mutation
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "Lifecycle-audit 2026-07-13 — H5 mutation-path unification (C6b dependency)"
tags: [section/slices, status/in-progress, type/slice, lifecycle, atomicity, refactor]
updated: 2026-07-14
reviewed: false
depends-on: ["[[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]]"]
blocks: []
issue: 439
---

# Slice: H5 — unify all lifecycle state mutation behind LifecycleTransitionCoordinator

Route **every** production lifecycle state mutation through the single `LifecycleTransitionCoordinator`
boundary that C6b introduces. Surfaced by C6b's structural inventory (Yua correction G): C6b makes
Qdrant↔SQLite transitions atomic only for callers that go through the coordinator, but state is mutated
directly by many bypassing paths today.

## The bypass inventory (verified 2026-07-13)

**State-writing** `set_payload` sites (verified 2026-07-13 by an AST rule — a `set_payload` in a function
that writes a `state` field; non-state sites like `maturation.py:893` tags/importance,
`synthesis.py:718` contradicts, `demotion.py:380` reinforcement-clock are correctly EXCLUDED). Every one
bypasses any coordinator and can produce mutation-without-audit:

- **5 plane `transition()` methods** — `planes/episodic/plane.py:812`, `planes/concept/plane.py:436`,
  `planes/thoughts/plane.py:488`, `planes/artifact/plane.py:295`, `planes/curated/plane.py:449` (each
  `set_payload`s `state`+`version` and emits its own `LifecycleEvent`), called by
  `lifecycle/promotion.py`, `lifecycle/demotion.py` (×5), `api/routers/writes_concept.py` (×2).
- **`lifecycle/transitions.py:252`** — the canonical path.

6 state-writing TRANSITION sites across 6 files — the C6b G1 guard's present-denominator control pins
exactly these. **Out of scope here:** `planes/curated/plane.py:224` (`create`) is an INITIAL-state write
(capture/create atomicity, M9 / a deliberately-approved C6b extension), not a transition — H5 does NOT
force it through the transition coordinator.

## Scope

Migrate all of the above to `LifecycleTransitionCoordinator`. The mechanical guard shipped RED in C6b
([[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] guard G1: AST/rg forbidding direct
`state`-writing `set_payload` outside the coordinator) flips green when this slice lands.

The migration is a three-way outcome migration, not merely a syntactic delegation:

- each plane `transition()` requires a coordinator and returns a consumed
  `Result[TransitionResult | TransitionPending, TransitionError]`;
- `Pending` is preserved as deferred work (never fabricated into the historical tuple success shape);
- `Err` remains terminal and typed;
- `Final` is the only arm that may run completed/dependent work;
- concept promotion carries `promoted_to` and `promoted_at` in the coordinator's deterministic intended
  patch, version fence, readback digest, and replay path. A post-Final second `set_payload` is forbidden;
- the two concept HTTP callers map Pending to the same exact typed 202 body used by the four S7 routes.

## Owned paths

- `docs/Musubi/_slices/slice-h5-unify-state-mutation.md`
- `docs/Musubi/_slices/slice-c6b-phase1-source-impl.md` (closeout metadata only)
- `docs/Musubi/_inbox/locks/slice-h5-unify-state-mutation.lock`
- `docs/Musubi/_inbox/locks/slice-c6b-phase1-source-impl.lock` (retire merged predecessor lock only)
- `docs/Musubi/13-decisions/h5-canonical-plane-transition-design.md`
- `src/musubi/planes/episodic/plane.py`
- `src/musubi/planes/concept/plane.py`
- `src/musubi/planes/thoughts/plane.py`
- `src/musubi/planes/artifact/plane.py`
- `src/musubi/planes/curated/plane.py`
- `src/musubi/lifecycle/coordinator.py`
- `src/musubi/lifecycle/transitions.py`
- `src/musubi/lifecycle/promotion.py`
- `src/musubi/lifecycle/demotion.py`
- `src/musubi/lifecycle/runner.py`
- `src/musubi/api/bootstrap.py`
- `src/musubi/api/dependencies.py`
- `src/musubi/api/lifecycle_responses.py`
- `src/musubi/api/routers/writes_concept.py`
- `openapi.yaml`
- `tests/lifecycle/test_h5_unify_state_mutation.py`
- `tests/lifecycle/test_c6b_atomicity.py`
- `tests/lifecycle/test_promotion.py`
- `tests/lifecycle/test_demotion.py`
- `tests/api/test_concept_writes.py`
- `tests/planes/test_thoughts.py` (required-coordinator compatibility migration only)
- `tests/planes/test_episodic.py` (required-coordinator compatibility migration only)
- `tests/planes/test_curated.py` (required-coordinator compatibility migration only)
- `tests/planes/test_concept.py` (required-coordinator compatibility migration only)
- `tests/planes/test_artifact.py` (required-coordinator compatibility migration only)
- `tests/lifecycle/test_lifecycle.py` (required-coordinator compatibility migration only)
- `tests/lifecycle/test_reflection.py` (required-coordinator compatibility migration only)
- `tests/lifecycle/test_synthesis.py` (required-coordinator compatibility migration only)
- `tests/lifecycle/test_maturation.py` (required-coordinator compatibility migration only)
- `tests/api/test_retrieve_wildcards.py` (required-coordinator compatibility migration only)
- `tests/api/test_retrieve_recent.py` (required-coordinator compatibility migration only)
- `tests/api/test_api_v0_write.py` (required-coordinator compatibility migration only)
- `tests/api/test_api_v0_read.py` (required-coordinator compatibility migration only)

## Forbidden paths

- deployment and migration paths (owned by the subsequent FILE-to-DIR migration gate)
- adapters and retrieval code
- plane create/capture methods and curated initial-state writes
- C6 durable sink acceptance/flush behavior

## Specs to implement

- [[13-decisions/h5-canonical-plane-transition-design]]

## Test Contract

1. `test_h5_g1_no_direct_state_transition_setpayload_outside_coordinator`
2. `test_h5_present_denominator_is_empty_after_accounted_migration`
3. `test_h5_each_plane_transition_requires_coordinator_and_preserves_final_pending_err`
4. `test_h5_concept_promotion_receipt_is_in_the_atomic_intended_patch`
5. `test_h5_concept_promotion_receipt_participates_in_replay_and_full_readback`
6. `test_h5_promotion_pending_defers_notification_and_rejection`
7. `test_h5_promotion_final_runs_dependent_work_once`
8. `test_h5_demotion_pending_does_not_increment_completed`
9. `test_h5_demotion_final_increments_completed_once`
10. `test_h5_concept_promote_http_pending_is_typed_202`
11. `test_h5_concept_delete_http_pending_is_typed_202`
12. `test_h5_coordinator_result_is_consumed_at_every_migrated_caller`

## Relationship (acyclic — no circular dependency)

- **Depends on:** [[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] **Phase 1** — H5 consumes the
  `LifecycleTransitionCoordinator` API that C6b Phase 1 defines + implements.
- **Gates C6b closure (not a frontmatter `blocks` edge — that would be circular):** C6b Phase 1 lands
  with C6b still OPEN; H5 then migrates every mutation path; C6b's guard G1 (and G2/G3 for the migrated
  callers) go green; only THEN can C6b close as a defect. This "closure gate" is a documented state, not a
  DAG edge, per Yua's no-circular-dependency ruling (2026-07-13).

## Status

**`in-progress`** (2026-07-14) — C6b source phases S1-S7 merged on main at `dd0f971`; the dependency is
now satisfied. Tracking **Issue #439** (`status:in-progress`). The first H5 commit is contract/tests-only;
source follows only after the five-plane, caller-outcome, concept-receipt, and HTTP-202 reds discriminate
the unsafe alternatives.

## Work log

- 2026-07-14 — `codex-gpt5` claimed H5 after PR #455 merged. Re-derived the live denominator as the five
  plane `transition()` methods. Locked three-way plane/caller semantics and the concept promotion receipt
  into the test contract before source work; release/deploy remains held behind H5 and the storage migration.
- 2026-07-14 — The required coordinator/typed Result API correctly invalidated the historical tuple-return
  assumptions in 13 completed-slice test modules. Added those files to H5 for compatibility migration only
  after confirming their owning feature slices are complete (the one RET-003 API wire slice remains ready,
  not active) and the slice validator reports ownership overlap as warnings rather than an active lock error.
- 2026-07-14 — Completed the compatibility migration across the owned plane, lifecycle, and API suites.
  Full `make check` passed with 1,927 passed, 197 skipped, 17 deselected, and three expected xfails at
  89.01% coverage; `make agent-check` completed with warnings only. Linked the H5 decision's identical
  12-bullet Test Contract so the mechanical closure audit evaluates this slice's actual acceptance surface.
