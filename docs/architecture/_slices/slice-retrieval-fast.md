---
title: "Slice: Fast-path retrieval"
slice_id: slice-retrieval-fast
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "3 Reranker"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]", "[[_slices/slice-plane-episodic]]"]
blocks: ["[[_slices/slice-adapter-livekit]]"]
---

# Slice: Fast-path retrieval

> Latency-budgeted (<400ms) episodic-only retrieval path. Cached; no cross-plane orchestration.

**Phase:** 3 Reranker · **Status:** `in-review` · **Owner:** `codex-gpt5`

## Specs to implement

- [[05-retrieval/fast-path]]

## Owned paths (you MAY write here)

- `src/musubi/retrieve/fast.py`
- `tests/retrieve/test_fast.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/retrieve/hybrid.py`    (owned by slice-retrieval-hybrid, done)
- `src/musubi/retrieve/scoring.py`   (owned by slice-retrieval-scoring, done)
- `src/musubi/retrieve/rerank.py`    (owned by slice-retrieval-rerank, done)
- `src/musubi/retrieve/deep.py`      (owned by slice-retrieval-deep, in-flight — Hana)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-retrieval-hybrid]]
- [[_slices/slice-retrieval-scoring]]
- [[_slices/slice-plane-episodic]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-adapter-livekit]]

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

### 2026-04-19 17:49 — codex-gpt5 — claimed slice

- Claimed Issue #28 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-19 18:18 — codex-gpt5 — handoff to in-review

- Added `src/musubi/retrieve/fast.py` as the latency-budgeted retrieval path composing `hybrid_search` + `score`, with optional response cache, embedding cache threading, plane fanout timeouts, partial-result warnings, typed errors, snippet packing, and lineage summaries.
- Added `tests/retrieve/test_fast.py` with the spec Test Contract realized as passing unit/property tests plus two deferred integration bullets.
- Verification: `make check` green; `make tc-coverage SLICE=slice-retrieval-fast` green; `make agent-check` reported warnings only and no `✗` hard errors; `uv run coverage report --include='src/musubi/retrieve/*'` reports 99% total retrieve coverage and 99% on `fast.py`.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_fast_path_p50_under_150ms_on_10k_corpus` | ✓ passing | `tests/retrieve/test_fast.py:90` |
| `test_fast_path_returns_results_in_score_desc` | ✓ passing | `tests/retrieve/test_fast.py:115` |
| `test_fast_path_applies_namespace_filter` | ✓ passing | `tests/retrieve/test_fast.py:136` |
| `test_fast_path_applies_state_matured_default` | ✓ passing | `tests/retrieve/test_fast.py:160` |
| `test_fast_path_runs_planes_concurrently` | ✓ passing | `tests/retrieve/test_fast.py:184` |
| `test_fast_path_timeout_on_one_plane_returns_partial_with_warning` | ✓ passing | `tests/retrieve/test_fast.py:208` |
| `test_fast_path_tei_timeout_returns_503` | ✓ passing | `tests/retrieve/test_fast.py:235` |
| `test_fast_path_qdrant_down_returns_503` | ✓ passing | `tests/retrieve/test_fast.py:257` |
| `test_fast_path_empty_corpus_returns_empty_200` | ✓ passing | `tests/retrieve/test_fast.py:279` |
| `test_fast_path_response_cache_hits_within_30s` | ✓ passing | `tests/retrieve/test_fast.py:301` |
| `test_fast_path_response_cache_disabled_by_default` | ✓ passing | `tests/retrieve/test_fast.py:377` |
| `test_fast_path_embedding_cache_always_on` | ✓ passing | `tests/retrieve/test_fast.py:405` |
| `test_fast_path_snippet_max_200_chars` | ✓ passing | `tests/retrieve/test_fast.py:429` |
| `test_fast_path_lineage_summary_present_not_hydrated` | ✓ passing | `tests/retrieve/test_fast.py:450` |
| `test_fast_path_does_not_call_reranker` | ✓ passing | `tests/retrieve/test_fast.py:684` |
| `hypothesis: same query on same corpus returns identical results` | ✓ passing property test | `tests/retrieve/test_fast.py:691` |
| `hypothesis: limit parameter is honored exactly` | ✓ passing property test | `tests/retrieve/test_fast.py:705` |
| `integration: LiveKit Fast-Talker scenario: voice-like queries p95 ≤ 400ms` | ⏭ skipped (slice-retrieval-evals: LiveKit scenario needs perf harness) | `tests/retrieve/test_fast.py:715` |
| `integration: degradation scenario — kill sparse TEI mid-request, response still returns with warnings` | ⏭ skipped (slice-ops-observability: live sparse TEI kill test needs services) | `tests/retrieve/test_fast.py:720` |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- [PR #74](https://github.com/ericmey/musubi/pull/74)
