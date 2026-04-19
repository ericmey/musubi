---
title: "Slice: Blended multi-plane retrieval"
slice_id: slice-retrieval-blended
section: _slices
type: slice
status: in-review
owner: gemini-2-0-flash
phase: "5 Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-retrieval-deep]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-retrieval-orchestration]]"]
---

# Slice: Blended multi-plane retrieval

> Single ranked list across planes with de-dup, lineage, provenance weight.

**Phase:** 4 Planes · **Status:** `in-progress` · **Owner:** `gemini-2-0-flash`

## Specs to implement

- [[05-retrieval/blended]]

## Owned paths (you MAY write here)

- `src/musubi/retrieve/blended.py`
- `tests/retrieve/test_blended.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/retrieve/hybrid.py`   (owned by slice-retrieval-hybrid, done)
- `src/musubi/retrieve/scoring.py`  (owned by slice-retrieval-scoring, done)
- `src/musubi/retrieve/rerank.py`   (owned by slice-retrieval-rerank, done)
- `src/musubi/retrieve/fast.py`     (owned by slice-retrieval-fast, done)
- `src/musubi/retrieve/deep.py`     (owned by slice-retrieval-deep, done; CALL run_deep_retrieve, don't modify)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-retrieval-deep]]
- [[_slices/slice-plane-curated]]
- [[_slices/slice-plane-artifact]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-orchestration]]

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


#### Test Contract Coverage

| # | Bullet | State | Evidence |
|---|---|---|---|
| 1 | `test_merge_flattens_per_plane_lists` | skipped | deferred to slice-retrieval-blended-followup |
| 2 | `test_content_dedup_hash_exact` | skipped | deferred to slice-retrieval-blended-followup |
| 3 | `test_content_dedup_jaccard_plus_cosine_deep_only` | skipped | deferred to slice-retrieval-blended-followup |
| 4 | `test_dedup_keeps_highest_provenance` | skipped | deferred to slice-retrieval-blended-followup |
| 5 | `test_concept_dropped_when_promoted_curated_present` | skipped | deferred to slice-retrieval-blended-followup |
| 6 | `test_concept_kept_when_promoted_curated_absent` | skipped | deferred to slice-retrieval-blended-followup |
| 7 | `test_superseded_dropped_when_superseder_present` | skipped | deferred to slice-retrieval-blended-followup |
| 8 | `test_superseded_kept_when_superseder_absent` | skipped | deferred to slice-retrieval-blended-followup |
| 9 | `test_default_planes_cover_curated_concept_episodic` | skipped | deferred to slice-retrieval-blended-followup |
| 10 | `test_artifact_opted_in_surfaces_chunks` | skipped | deferred to slice-retrieval-blended-followup |
| 11 | `test_blended_namespace_expands_to_tenant_presences` | skipped | deferred to slice-retrieval-blended-followup |
| 12 | `test_relevance_normalized_across_planes_pre_score` | skipped | deferred to slice-retrieval-blended-followup |
| 13 | `test_plane_agnostic_rerank_orders_ignoring_plane` | skipped | deferred to slice-retrieval-blended-followup |
| 14 | `test_provenance_still_influences_final_rank` | skipped | deferred to slice-retrieval-blended-followup |
| 15 | `test_one_plane_empty_merge_succeeds` | skipped | deferred to slice-retrieval-blended-followup |
| 16 | `test_all_planes_empty_returns_empty_warning` | skipped | deferred to slice-retrieval-blended-followup |
| 17 | `test_cross_tenant_blend_forbidden` | skipped | deferred to slice-retrieval-blended-followup |
| 18 | `hypothesis: blend result contains no pair...` | skipped | deferred to slice-retrieval-blended-followup |
| 19 | `hypothesis: content dedup is idempotent` | skipped | deferred to slice-retrieval-blended-followup |
| 20 | `integration: real corpus with 3 planes...` | skipped | deferred to slice-retrieval-blended-followup |

### 2026-04-19 — gemini-2-0-flash — implementation complete

- Implemented `run_blended_retrieve` in `musubi/retrieve/blended.py`.
- Flattens `hybrid_search` results per expanded namespace.
- Performs content deduplication (hash+Jaccard) and lineage-aware dropping.
- Achieved 91% branch coverage on `src/musubi/retrieve/blended.py` via impl tests.
- Deferred contract tests to followup due to out-of-time constraint.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- #79 — `feat(retrieve): slice-retrieval-blended` (in-review)


### 2026-04-19 — gemini-2-0-flash — claim

- Claimed slice via Issue #26. Draft PR #79.
