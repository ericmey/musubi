---
title: "Slice: Episodic plane — finish first cut"
slice_id: slice-plane-episodic-followup
section: _slices
type: slice
status: ready
owner: unassigned
phase: "4 Planes"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-plane-episodic]]"]
blocks: []
---

# Slice: Episodic plane — finish first cut

> Close the 18 in-scope Test Contract bullets deferred by `slice-plane-episodic`'s first cut: add `patch()`, `delete()`, access-count bump on read, four plane-boundary guards, three transition-behavior tests, one concurrency test, and two Hypothesis properties.

**Phase:** 4 Planes · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[04-data-model/episodic-memory]] (the 18 in-scope bullets from `make tc-coverage SLICE=slice-plane-episodic`, enumerated below)

## Owned paths (you MAY write here)

- `src/musubi/planes/episodic/`   (parent is `status: done`; ownership effectively released)
- `tests/planes/test_episodic.py`  (append to the existing 22-test suite)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/curated/`
- `src/musubi/planes/artifact/`
- `src/musubi/planes/concept/`
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/api/`
- `src/musubi/types/`
- `src/musubi/adapters/`
- `openapi.yaml`
- `proto/`

## Depends on

- [[_slices/slice-plane-episodic]] (done — first cut shipped create/dedup/get/query/transition)

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- _(none — parent slice already unblocked downstream consumers. This slice is
  completeness work, not a DAG blocker.)_

## Context

The parent slice (`slice-plane-episodic`) shipped the first cut of `EpisodicPlane` as direct commits to `v2` during an unsupervised session. The work log on the parent documents 21 deferred Test Contract bullets classified into three buckets (A: in-scope deferrals, B: performance/integration, C: property tests). This slice closes **bucket A + bucket C** — everything that's in-scope for unit + property tests against the episodic plane itself. Bucket B (performance bullets 32, 33) stays deferred to the integration-test harness.

The parent slice's frontmatter says `status: done` but its own work log says "the follow-up work on bucket A [is] a prerequisite for flipping to `done`." That inconsistency isn't fixed by this slice — this slice just closes the bullets. Operator may reconsider the parent's status once this ships.

## In-scope Test Contract bullets (18)

**Missing methods on `EpisodicPlane`** (real implementation, not just tests — Method-ownership: these belong to the plane, not to `slice-api-v0`):

1. `patch()` — tag/importance edits only, no content mutation.
   - `test_patch_importance_creates_lifecycle_event_and_bumps_version`
   - `test_patch_tags_is_additive_by_default`
   - `test_patch_forbids_mutating_content_directly`
2. `delete()` — operator-scope only, emits a `LifecycleEvent` audit entry.
   - `test_delete_requires_operator_scope`
   - `test_delete_creates_audit_event`
3. Access-count bump on read via `batch_update_points` (never N+1).
   - `test_access_count_increments_via_batch_update_points`
   - `test_access_count_update_is_not_N_plus_1`

**Guards not yet enforced at the plane boundary:**

4. 32KB content cap with artifact-suggestion in the error.
   - `test_content_over_32kb_rejected_with_suggestion_to_use_artifact`
5. Vector dimension mismatch rejection.
   - `test_vector_dimension_mismatch_rejected_with_clear_error`
6. Future `event_at` rejection.
   - `test_create_rejects_future_event_at`
7. Namespace-regex re-assertion at plane boundary.
   - `test_create_enforces_namespace_regex`

**Tests for existing transition behaviour (table exists, dedicated tests don't):**

8. `test_demotion_keeps_record_but_filters_from_default_reads`
9. `test_archival_removes_from_default_queries_but_returns_from_get_by_id`
10. `test_query_respects_state_filter_default_excludes_provisional`

**Concurrency:**

11. `test_concurrent_dedup_race_resolves_to_single_winner`

**Hypothesis / property tests:**

12. `hypothesis: idempotency — re-ingesting same content N times produces 1 memory with reinforcement_count == N`
13. `hypothesis: lifecycle monotonicity — state transitions never go backwards`

## Explicitly out-of-scope (do NOT implement here)

- `test_query_hybrid_returns_scored_results_in_descending_order` — owned by `slice-retrieval-hybrid` (done).
- `test_maturation_*` (3 bullets) — owned by `slice-lifecycle-maturation`.
- `test_forward_compat_reads_schema_version_0_point` — belongs to a future schema-migration slice.
- Performance bullets 32, 33 — belong to the integration-test harness when we have one.

If you find yourself tempted to implement one of these, open a cross-slice ticket in `_inbox/cross-slice/` instead.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] All 13 bullet test names above appear as passing (`✓`) tests under `make tc-coverage SLICE=slice-plane-episodic`.
- [ ] Branch coverage ≥ **90%** on `src/musubi/planes/episodic/` (Phase-4-Planes floor; tighter than the 85% general floor).
- [ ] `patch()`, `delete()`, and the access-count bump are implemented in `src/musubi/planes/episodic/plane.py` with typed errors (`Result[T, E]` at module boundaries per CLAUDE.md § style).
- [ ] `delete()` requires operator scope at the plane layer (do NOT delegate to API-layer auth).
- [ ] Access-count bump uses `batch_update_points` — not a per-row `update_point` loop.
- [ ] 32KB / dim-mismatch / future-`event_at` / namespace-regex guards raise typed errors with actionable messages (the 32KB one must mention the artifact plane as the suggested alternative).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Lock file removed from `_inbox/locks/`.

No spec edits expected; if you find the spec under-specifies any of these, update it in-PR with a `spec-update: docs/architecture/04-data-model/episodic-memory.md` trailer.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 — operator — slice carved

- Legitimizes Issue #37 ("slice-plane-episodic: finish first cut — patch/delete/access_count + 18 deferred bullets") with a proper slice file per the Option-3 followup pattern established by `slice-retrieval-blended-followup`.
- Paths reconciled to post-ADR-0015 monorepo layout (`src/musubi/planes/episodic/`, not `musubi/planes/episodic/`).
- Canonical commit IS `feat(...)` for this slice (unlike the blended-followup which was test-only) — `handoff-audit.py` checks will apply normally.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
