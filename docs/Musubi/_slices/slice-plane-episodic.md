---
title: "Slice: Episodic plane"
slice_id: slice-plane-episodic
section: _slices
type: slice
status: done
owner: cowork-auto
phase: "4 Planes"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]"]
blocks: ["[[_slices/slice-api-app-bootstrap]]", "[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-v0-write]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-lifecycle-maturation]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-episodic-followup]]", "[[_slices/slice-poc-data-migration]]", "[[_slices/slice-retrieval-fast]]"]
---
# Slice: Episodic plane

> Source-first time-indexed recollection. Qdrant-primary. Named dense + sparse vectors. Provisional → matured lifecycle.

**Phase:** 4 Planes · **Status:** `done` · **Owner:** `cowork-auto`

## Specs to implement

- [[04-data-model/episodic-memory]]

## Owned paths (you MAY write here)

- `src/musubi/planes/episodic/`
- `tests/planes/test_episodic.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/curated/`
- `src/musubi/planes/artifact/`
- `src/musubi/planes/concept/`
- `src/musubi/api/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-fast]]
- [[_slices/slice-lifecycle-maturation]]

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

### 2026-04-19 — operator — reconcile paths to post-ADR-0015 monorepo layout

- 7th pre-src-monorepo drift fix. `owns_paths` was `musubi/planes/episodic/`; reconciled to `src/musubi/planes/episodic/`. Sibling-plane forbidden paths also reprefixed; added `concept/` to the forbidden list (it was missing from the original seed).
- Carved [[_slices/slice-plane-episodic-followup]] to legitimize Issue #37 (18 in-scope Test Contract bullets + `patch`/`delete`/access-count methods + plane-boundary guards). Followup pattern matches [[_slices/slice-retrieval-blended-followup]].
- Parent `status: done` flag is preserved, per the pattern established with `slice-retrieval-blended` → `slice-retrieval-blended-followup`.

### 2026-04-18 — cowork-auto — first cut landed; `status: ready → in-review`

Cowork shipped the first cut during the unsupervised session on 2026-04-18, landing as direct commits to `v2` (pre-branch-protection, pre-full-PR-lifecycle). Commits:

- `bbc0f5b` — `test(planes): initial test contract for slice-plane-episodic`
- `05c1797` — `feat(planes): first cut of the episodic plane for slice-plane-episodic`

Delivery:
- `src/musubi/planes/episodic/plane.py` (329 LoC) — `EpisodicPlane` class with `create()` (dedup at 0.92 cosine sim → reinforce, else insert), `get()` (namespace-scoped fetch), `query()` (dense-only filtered retrieval, `include_demoted` opt-in), `transition()` (state change → emits `LifecycleEvent`, round-trips through `model_validate` to preserve monotonicity invariants).
- `tests/planes/test_episodic.py` (411 LoC, 22 tests) — dedup hit/miss paths, tag merging, reinforcement count + version bumps, namespace isolation on both read + write, transition happy + illegal paths, query + include_demoted.

Test Contract Closure state via `make tc-coverage SLICE=slice-plane-episodic`: **12 ✓ passing / 2 ⊘ non-test / 21 ✗ missing** out of 35 spec bullets.

The 21 missing bullets fall into three buckets:

**A) In-scope deferrals — tracked in a follow-up Issue (see Cross-slice tickets below):**
- `test_create_enforces_namespace_regex`, `test_create_rejects_future_event_at` — input validation, currently via pydantic type validators on `EpisodicMemory`; could be explicitly re-asserted at the plane boundary.
- `test_maturation_*` (3 bullets) — belongs to `slice-lifecycle-maturation`, which is the right home but not yet built.
- `test_archival_*`, `test_demotion_*` — partly covered by `transition()` tests; explicit archival flow tests still needed.
- `test_access_count_*` (2 bullets) — bump-on-read behaviour not yet implemented.
- `test_patch_*` (3 bullets) — `EpisodicPlane.patch()` method not yet implemented (Method-ownership: this is the plane's, not API's).
- `test_delete_*` (2 bullets) — `EpisodicPlane.delete()` method not yet implemented (operator-scope only).
- `test_query_hybrid_returns_scored_results_in_descending_order` — currently dense-only; hybrid fusion belongs to `slice-retrieval-hybrid`.
- `test_query_respects_state_filter_default_excludes_provisional` — partially covered but not as a dedicated test.
- `test_content_over_32kb_rejected_with_suggestion_to_use_artifact` — 32KB cap not yet enforced.
- `test_concurrent_dedup_race_resolves_to_single_winner` — concurrency test deferred.
- `test_vector_dimension_mismatch_rejected_with_clear_error` — dimension guard deferred.
- `test_forward_compat_reads_schema_version_0_point` — POC-compat migration concern; belongs to a future migration slice.

**B) Performance — integration tests (bullets 32, 33):** deferred to the integration-test harness when we have a reference host running; out-of-scope for unit tests.

**C) Property / hypothesis tests (bullets 34, 35):** `⊘ non-test` per `tc_coverage.py`'s classifier; in-scope for this slice eventually but typically layered after the core CRUD passes — tracked in the same follow-up Issue as bucket A.

The review pass + the follow-up work on bucket A are both prerequisites for flipping to `done`. Bucket B is deferred to the integration harness; bucket C can be layered in the follow-up or a later polish pass.

## Cross-slice tickets opened by this slice

- [[_slices/slice-plane-episodic-followup]] — closes the 13 bucket-A + bucket-C Test Contract bullets (Issue #37). Parent's `status: done` flag predates the followup slice; see followup slice's Context section.

## PR links

- _(direct commits to v2 on 2026-04-18, pre-branch-protection — see commits `bbc0f5b` (test) and `05c1797` (feat) in the work log above)_
