---
title: "Slice: Retrieval scoring model"
slice_id: slice-retrieval-scoring
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "2 Hybrid"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-retrieval-fast]]", "[[_slices/slice-retrieval-deep]]", "[[_slices/slice-retrieval-blended]]"]
---

# Slice: Retrieval scoring model

> The single function that turns a raw hit into a rank-orderable number. Weights: relevance, recency, importance, maturity, reinforcement, provenance, penalties.

**Phase:** 2 Hybrid · **Status:** `done` · **Owner:** `codex-gpt5`

## Specs to implement

- [[05-retrieval/scoring-model]]

## Owned paths (you MAY write here)

- `musubi/retrieve/scoring.py`
- `tests/retrieve/test_scoring.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/api/`
- `musubi/planes/`

## Depends on

- [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-fast]]
- [[_slices/slice-retrieval-deep]]
- [[_slices/slice-retrieval-blended]]

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

### 2026-04-19 13:33 — codex-gpt5 — claimed slice

- Claimed Issue #32 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-19 13:47 — codex-gpt5 — handoff to in-review

- Added `src/musubi/retrieve/scoring.py` with deterministic `Hit`, `ScoreWeights`, `ScoreComponents`, `ScoredHit`, `score()`, `score_result()`, and `rank_hits()` APIs.
- Implemented relevance normalization, rerank-score sigmoid normalization, plane-specific recency half-lives, importance clamp, provenance table, reinforcement/access log scaling, component exposure, and deterministic `(object_id, plane)` tiebreaks.
- Added `tests/retrieve/test_scoring.py` for the scoring Test Contract. Focused coverage for `src/musubi/retrieve/scoring.py`: 100%; retrieval include coverage: 99%.
- Verification: `make check` passed; `uv run coverage report --include='src/musubi/retrieve/*'` passed with 99% retrieval coverage.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_score_in_0_1_range_for_any_hit` | ✓ passing | `tests/retrieve/test_scoring.py:58` |
| `test_components_sum_with_weights_equals_total` | ✓ passing | `tests/retrieve/test_scoring.py:80` |
| `test_relevance_normalized_within_batch` | ✓ passing | `tests/retrieve/test_scoring.py:104` |
| `test_recency_decay_matches_half_life_table` | ✓ passing | `tests/retrieve/test_scoring.py:125` |
| `test_recency_half_life_per_plane_applied` | ✓ passing | `tests/retrieve/test_scoring.py:137` |
| `test_importance_clamped_to_1_10` | ✓ passing | `tests/retrieve/test_scoring.py:152` |
| `test_provenance_values_match_table` | ✓ passing | `tests/retrieve/test_scoring.py:173` |
| `test_provenance_demoted_states_get_0_1` | ✓ passing | `tests/retrieve/test_scoring.py:180` |
| `test_reinforcement_log_scaled` | ✓ passing | `tests/retrieve/test_scoring.py:192` |
| `test_tiebreak_deterministic_on_object_id` | ✓ passing | `tests/retrieve/test_scoring.py:212` |
| `test_score_components_exposed_on_result` | ✓ passing | `tests/retrieve/test_scoring.py:238` |
| `test_weights_change_shifts_ranking_predictably` | ✓ passing | `tests/retrieve/test_scoring.py:251` |
| `test_no_rng_used_in_scoring` | ✓ passing | `tests/retrieve/test_scoring.py:286` |
| `hypothesis: scores are monotonic in each component holding others fixed` | ✓ passing property test; declared here for tc_coverage non-test handling | `tests/retrieve/test_scoring.py:296` |
| `hypothesis: swapping weights reorders results consistently with the math` | ✓ passing property test; declared here for tc_coverage non-test handling | `tests/retrieve/test_scoring.py:324` |
| `eval: golden query set MRR ≥ 0.7 with default weights` | ⏭ skipped (slice-retrieval-evals: golden query set lives there) | `tests/retrieve/test_scoring.py:380` |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- PR #54 — feat(retrieve): slice-retrieval-scoring
