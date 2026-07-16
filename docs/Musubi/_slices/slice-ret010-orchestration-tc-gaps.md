---
title: "Slice: RET-010 — close legacy orchestration Test Contract gaps"
slice_id: slice-ret010-orchestration-tc-gaps
issue: 509
section: _slices
type: slice
status: in-progress
owner: cursor-grok
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice, retrieval, orchestration]
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---

# Slice: RET-010 — close legacy orchestration Test Contract gaps

Tracks #509. Successor to `slice-retrieval-orchestration` (already `done`); this slice closes the remaining Closure Rule debt on [[05-retrieval/orchestration]] without redesigning retrieval semantics.

## What

`docs/Musubi/05-retrieval/orchestration.md` has named unit-contract bullets (and integration bullets) with incomplete closure evidence. `tests/retrieve/test_orchestration.py` currently holds only a few error-path / integration placeholders; structural / concurrency / timeout / determinism proofs were deleted after fast/deep grew dedicated suites under different names.

Every legacy bullet must reach an honest Closure Rule state:

1. **Passing test** whose name matches the bullet verbatim, or
2. **`@pytest.mark.skip` / `xfail`** with `deferred to slice-<id>: <why>`, or
3. **Declared out-of-scope** in this slice's Work log with a named follow-up Issue.

Do **not** redesign retrieval semantics merely to satisfy test names. Prefer proving existing promised behavior at the orchestrator boundary (or documenting that coverage lives in `test_fast.py` / `test_deep.py` via an honest skip/out-of-scope entry that names the evidence).

## Specs to implement

- [[05-retrieval/orchestration]] — `## Test Contract`

## Owned paths

- `tests/retrieve/test_orchestration.py`
- `src/musubi/retrieve/orchestration.py` (only if a unit promise is missing at the facade and a minimal fix is required)
- `docs/Musubi/_slices/slice-ret010-orchestration-tc-gaps.md` (this file)
- `docs/Musubi/_inbox/locks/slice-ret010-orchestration-tc-gaps.lock`

## Forbidden paths

- `src/musubi/retrieve/fast.py`, `deep.py`, `hybrid.py`, `scoring.py`, `rerank.py`, `blended.py`, `recent.py` — open a cross-slice ticket if a proof requires production changes there
- `src/musubi/api/`, `openapi.yaml`, `proto/`, `src/musubi/types/`
- Redesigning scoring weights, timeout budgets, or mode dispatch
- Live host / GPU / TEI contact for integration bullets (route those to existing harnesses)

## Test Contract

Transcribed from [[05-retrieval/orchestration]] `## Test Contract` (unit + error + integration). Function names must match verbatim.

Structural:
1. `test_fast_mode_skips_rerank`
2. `test_deep_mode_invokes_rerank`
3. `test_fast_mode_skips_lineage_hydrate`
4. `test_deep_mode_hydrates_when_flag_true`
5. `test_steps_run_in_documented_order`

Concurrency:
6. `test_planes_run_in_parallel`
7. `test_hydrate_fetches_run_in_parallel`

Timeouts:
8. `test_whole_call_timeout_fast_400ms`
9. `test_per_plane_timeout_deep_1500ms`
10. `test_rerank_timeout_returns_with_warning`

Determinism:
11. `test_deterministic_for_fixed_inputs`
12. `test_tiebreak_on_object_id`

Error paths:
13. `test_bad_query_returns_typed_error`
14. `test_forbidden_namespace_returns_typed_error`
15. `test_partial_plane_failure_returns_partial_with_warning`

Integration (environment-dependent — prefer skip to named harness if not runnable in unit CI):
16. `integration: end-to-end fast-path on 10K corpus with real TEI + Qdrant, p95 ≤ 400ms`
17. `integration: end-to-end deep-path with rerank, NDCG@10 on golden set ≥ threshold`
18. `integration: kill TEI mid-request, pipeline returns with documented degradation`

## Definition of Done

- Every Test Contract bullet above is in Closure Rule state 1, 2, or 3.
- No silent omissions; `make tc-coverage SLICE=slice-ret010-orchestration-tc-gaps` exits 0.
- `make check` + `make agent-check` green.
- PR body first line is `Closes #509.`
- Frontmatter + Issue Dual-updated through handoff.

## Work log

### 2026-07-15 — cursor-grok — claim

- Claimed Issue #509 via Dual-update (`status:ready` → `status:in-progress`, assignee `@me`).
- Created successor slice `slice-ret010-orchestration-tc-gaps` (original `slice-retrieval-orchestration` remains `done`).
- Branch `slice/ret010-orchestration-tc-gaps`; draft PR #580. Next: tests-first closure of the orchestration Test Contract.
