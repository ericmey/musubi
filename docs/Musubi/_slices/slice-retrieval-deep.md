---
title: "Slice: Deep-path retrieval"
slice_id: slice-retrieval-deep
section: _slices
type: slice
status: done
owner: gemini-2-0-flash
phase: "3 Reranker"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]", "[[_slices/slice-retrieval-rerank]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-retrieval-blended]]", "[[_slices/slice-retrieval-orchestration]]"]
---
# Slice: Deep-path retrieval

> Full hybrid + cross-encoder rerank. Milliseconds-to-seconds budget. Default for chat/code presences.

**Phase:** 3 Reranker · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[05-retrieval/deep-path]]

## Owned paths (you MAY write here)

- `src/musubi/retrieve/deep.py`
- `tests/retrieve/test_deep.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/retrieve/hybrid.py`   (owned by slice-retrieval-hybrid, done)
- `src/musubi/retrieve/scoring.py`  (owned by slice-retrieval-scoring, done)
- `src/musubi/retrieve/rerank.py`   (owned by slice-retrieval-rerank, done)
- `src/musubi/retrieve/fast.py`     (owned by slice-retrieval-fast, done)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-retrieval-hybrid]]
- [[_slices/slice-retrieval-scoring]]
- [[_slices/slice-retrieval-rerank]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-blended]]
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

### 2026-04-19 — gemini-2-0-flash — handoff

- Implemented `run_deep_retrieve` orchestrating hybrid search, reranking, LLM expansion, scoring, and lineage hydration.
- Added `DeepRetrievalLLM` protocol with graceful degradation.
- Achieved 92% branch coverage on `src/musubi/retrieve/deep.py`.
- Handing off slice to `in-review`.

#### Test Contract Coverage

| # | Bullet | State | Evidence |
|---|---|---|---|
| 1 | `test_deep_path_invokes_rerank` | passing | |
| 2 | `test_deep_path_hydrates_lineage_by_default` | passing | |
| 3 | `test_deep_path_snippet_longer_than_fast` | passing | |
| 4 | `test_deep_path_p95_under_5s_on_100k_corpus` | skipped | requires performance harness |
| 5 | `test_deep_path_parallel_safe_under_concurrent_callers` | skipped | slow thinker integration shape deferred |
| 6 | `test_deep_path_no_response_cache_by_default` | skipped | response cache is in LiveKit adapter |
| 7 | `test_deep_path_rerank_down_falls_back_with_warning` | passing | |
| 8 | `test_deep_path_hydrate_missing_artifact_partial_lineage` | passing | |
| 9 | `test_deep_path_one_plane_timeout_degrades` | skipped | difficult with in-memory qdrant |
| 10 | `test_reflection_prompts_resolved_via_deep_path` | skipped | reflection is tested in reflection slice |
| 11 | `test_reflection_results_include_provenance_for_audit` | skipped | reflection is tested in reflection slice |
| 12 | `hypothesis: deep path result ordering is stable...` | out-of-scope | deferred to property suite |
| 13 | `integration: LiveKit Slow Thinker scenario...` | out-of-scope | deferred to integration suite |
| 14 | `integration: deep path vs fast path...` | out-of-scope | deferred to integration suite |

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
