---
title: "Slice: Hybrid dense + sparse search"
slice_id: slice-retrieval-hybrid
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "2 Hybrid"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]", "[[_slices/slice-embedding]]"]
blocks: ["[[_slices/slice-retrieval-fast]]", "[[_slices/slice-retrieval-deep]]"]
---

# Slice: Hybrid dense + sparse search

> Qdrant Query API with server-side RRF fusion over named dense + sparse vectors.

**Phase:** 2 Hybrid · **Status:** `in-review` · **Owner:** `codex-gpt5`

## Specs to implement

- [[05-retrieval/hybrid-search]]

## Owned paths (you MAY write here)

- `musubi/retrieve/hybrid.py`
- `tests/retrieve/test_hybrid.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]
- [[_slices/slice-embedding]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-fast]]
- [[_slices/slice-retrieval-deep]]

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

### 2026-04-19 12:54 — codex-gpt5 — claimed slice

- Claimed Issue #29 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-19 13:06 — codex-gpt5 — handoff to in-review

- Added `src/musubi/retrieve/hybrid.py` with typed `HybridHit` / `RetrievalError`, query embedding cache, parallel dense+sparse encoding, Qdrant server-side RRF prefetch, namespace/state filter pushdown, sparse timeout fallback, hard query timeout, and multi-collection fanout helper.
- Added `tests/retrieve/test_hybrid.py` for the hybrid-search Test Contract. Focused coverage for `src/musubi/retrieve/hybrid.py`: 98%.
- Verification: `make check` passed; `make tc-coverage SLICE=slice-retrieval-hybrid` passed; `make agent-check` passed with warnings only.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_hybrid_query_uses_both_prefetch_steps` | ✓ passing | `tests/retrieve/test_hybrid.py:160` |
| `test_rrf_fusion_requested_server_side` | ✓ passing | `tests/retrieve/test_hybrid.py:173` |
| `test_namespace_filter_always_applied` | ✓ passing | `tests/retrieve/test_hybrid.py:183` |
| `test_prefetch_limit_comes_from_config` | ✓ passing | `tests/retrieve/test_hybrid.py:197` |
| `test_empty_query_returns_empty_not_error` | ✓ passing | `tests/retrieve/test_hybrid.py:210` |
| `test_query_encoding_runs_in_parallel` | ✓ passing | `tests/retrieve/test_hybrid.py:226` |
| `test_query_embedding_cache_hit_on_repeat` | ✓ passing | `tests/retrieve/test_hybrid.py:234` |
| `test_cache_cleared_on_model_version_change` | ✓ passing | `tests/retrieve/test_hybrid.py:249` |
| `test_hybrid_timeout_returns_partial_results` | ✓ passing | `tests/retrieve/test_hybrid.py:262` |
| `test_dense_only_fallback_when_sparse_timeout` | ✓ passing | `tests/retrieve/test_hybrid.py:278` |
| `test_fanout_over_planes_parallel` | ✓ passing | `tests/retrieve/test_hybrid.py:288` |
| `test_results_deduped_within_single_collection` | ✓ passing | `tests/retrieve/test_hybrid.py:306` |
| `test_filter_state_matured_excludes_archived_by_default` | ✓ passing | `tests/retrieve/test_hybrid.py:320` |
| `test_include_archived_opts_in` | ✓ passing | `tests/retrieve/test_hybrid.py:337` |
| `hypothesis: RRF result is deterministic for fixed (seed, corpus, query)` | ✓ passing property test; declared here for tc_coverage non-test handling | `tests/retrieve/test_hybrid.py:351` |
| `hypothesis: increasing prefetch_limit never reduces recall on fixed query` | ✓ passing property test; declared here for tc_coverage non-test handling | `tests/retrieve/test_hybrid.py:368` |
| `integration: BEIR-style eval on 1000-doc synthetic corpus, hybrid beats dense-only by ≥ 2 NDCG@10 points` | ⏭ skipped (slice-retrieval-evals: benchmark corpus belongs to eval suite) | `tests/retrieve/test_hybrid.py:383` |
| `integration: live Qdrant, hybrid with real BGE-M3 + SPLADE, p95 ≤ 150ms` | ⏭ skipped (slice-ops-gpu: live TEI/Qdrant p95 requires reference host) | `tests/retrieve/test_hybrid.py:392` |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- PR #50 — feat(retrieve): slice-retrieval-hybrid
