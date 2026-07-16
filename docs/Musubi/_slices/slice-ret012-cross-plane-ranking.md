---
title: "Slice: RET-012 cross-plane ranking globally comparable"
slice_id: slice-ret012-cross-plane-ranking
issue: 512
section: _slices
type: slice
status: done
owner: cowork-tama
phase: "Retrieval"
tags: [section/slices, status/done, type/slice]
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---

# Slice: RET-012 cross-plane ranking globally comparable

## What

Closes the cross-plane ranking global-comparability gap (Issue #512):
when a request fans out to more than one `(namespace, plane)` target,
the merged candidate list must use a single comparable relevance
calibration derived from the full candidate set, not per-target local
batch maxima.

The seam is a single post-fanout calibration step inserted into
``musubi.retrieve.orchestration._retrieve_uncounted``. It runs **before**
``best_by_id`` dedup, recomputes each hit's ``relevance`` against a
single global ``batch_max_rrf`` computed across the full pre-dedup
candidate set, rebuilds ``score`` from the new relevance plus the
existing intrinsic components, and lets the existing dedup then choose
the **highest-recalibrated** copy per ``object_id``. The final sort
key becomes ``(-score, object_id, plane)`` to make ordering
deterministic on ties.

The seam is gated on the multi-target branch only; the
``len(targets) == 1`` branch is bit-for-bit unchanged.

This slice is bounded to the retrieve seam:
- ``src/musubi/retrieve/scoring.py`` (add the calibration function)
- ``src/musubi/retrieve/orchestration.py`` (insert the seam, add the
  two optional raw fields to ``RetrievalResult``, populate them at
  the leg boundaries, update the final sort key)
- ``tests/retrieve/test_ret012_cross_plane_ranking.py`` (the 8-test
  contract)

## Why

Cross-plane fanout executes each ``(namespace, plane)`` target as an
independent single-plane retrieval, normalizes relevance against that
target's local batch maximum, then merges already-final scores
globally. A weak hit that is merely the best result in a weak plane
can therefore receive relevance ``1.0`` and outrank a materially
stronger hit from another plane. Per-plane scores are not reliably
comparable.

Evidence in the live code at ``fc05c7e``:

- ``src/musubi/retrieve/fast.py::_pack`` sets ``batch_max = max((hit.score
  for hit in hits), default=1.0)`` over the per-target candidate set.
- ``src/musubi/retrieve/deep.py::run_deep_retrieve`` sets the same per-leg
  ``batch_max_rrf`` on every ``Hit`` before calling ``rank_hits``.
- ``src/musubi/retrieve/scoring.py::_relevance`` divides ``rrf_score`` by
  that local ``batch_max_rrf`` (line 170-175) — a sole weak hit in a
  leg divides by itself and reaches ``1.0``.
- ``src/musubi/retrieve/orchestration.py::_retrieve_uncounted`` (multi-target
  branch) fans out per target, then sorts the merged ``best_by_id`` by
  ``r.score`` descending with **no object-id tie-break**.

The discriminating contract per the issue: a weak plane's sole hit
must not become maximally relevant merely by being alone.

## Contract

1. **Working-set global max** is the ``max`` of every leg's raw ``rrf_score``
   across the full pre-dedup fanout candidate set. No corpus-level
   percentile, no stored per-plane p99. The working set is the only
   input to the global max — that is what makes the calibration
   intrinsic.
2. **Sigmoid relevance is preserved.** When a leg carries a
   ``rerank_score`` (deep / blended mode), the seam's relevance is
   ``_sigmoid(rerank_score)`` — the same intrinsic function
   ``scoring._relevance`` already uses. The seam does not re-anchor
   the cross-encoder score against the global max. Coverage of the
   rerank path is a preservation guard, not a claimed RED.
3. **Calibrate BEFORE dedup.** The seam runs over the full pre-dedup
   candidate list, recomputes each hit's ``relevance`` and ``score``,
   then the existing ``best_by_id`` loop picks the highest-recalibrated
   copy per ``object_id``. Calibrating after dedup can permanently
   discard the better copy using the bad per-leg score.
4. **Deterministic final sort key is ``(-score, object_id, plane)``**
   AND the cross-plane dedup uses the same key to choose between
   equal-score copies of the same ``object_id``. The current multi-target
   sort (``sorted(..., key=lambda r: r.score, reverse=True)``) has no
   tie-break. The new sort key restores deterministic ordering for ties,
   matching the key ``scoring.rank_hits`` already uses for per-leg
   ranking. The dedup MUST use the same key — a strict
   ``hit.score > current.score`` would let the first-seen copy win on
   equal scores, leaking the gather order into the final result; the
   final sort could not repair it because one copy was already
   discarded by the dedup.
5. **Two optional internal fields on ``RetrievalResult``:**
   ``raw_rrf_score: float | None = None`` and
   ``raw_rerank_score: float | None = None``. Populated at the three
   leg boundaries (fast branch, ``_pack_scored_hits`` for deep / blended,
   recent branch is ``None`` / ``None``). **Not exposed on wire models**
   (``RankedResultRow``, ``RecentResultRow``, ``ContextPackItem``); the
   router's wire projection does not forward them.
6. **Single-target fast path bit-for-bit preserved.** The
   ``len(targets) == 1`` branch in ``_retrieve_uncounted`` is not
   touched. The seam only runs in the ``len(targets) > 1`` branch.
7. **Recent mode is a passthrough** at the seam. ``raw_rrf_score`` and
   ``raw_rerank_score`` are both ``None`` for recent rows; the seam's
   ``else`` branch leaves ``score_components`` and ``score`` unchanged.
   Recent's existing ``created_epoch`` ordering survives.
8. **Wildcard namespace fanout (ADR 0031)** is structurally the same
   multi-target branch; it inherits the seam for free. No new code
   path.

## Specs to implement

- [[05-retrieval/cross-plane-ranking]] (to be authored in the same PR;
  references the open Issue #512 and the bounded scope above).

## Acceptance

The first contract is bounded to nine bullets (ten items; bullet 5 is
parametrized over gather order) in
``tests/retrieve/test_ret012_cross_plane_ranking.py``: five RED
discriminating tests, four GREEN preservation guards. Test function
names transcribe the Test Contract bullets verbatim per the AGENTS.md
Test Contract Closure Rule.

### Test Contract (9 bullets, state 1 = passing at handoff)

1. `test_asymmetric_two_plane_fast_weak_sole_does_not_maximize` — RED
2. `test_three_plane_wildcard_uses_global_calibration` — RED
3. `test_pre_dedup_calibration_picks_higher_recalibrated_copy` — RED
4. `test_cross_plane_tiebreak_object_id_then_plane` — RED
5. `test_dedup_equal_score_prefers_lower_plane` — RED (parametrized over
   gather order: `curated_first`, `episodic_first`; same input must yield
   the same chosen copy)
6. `test_single_target_fast_path_unchanged` — GREEN guard
7. `test_rerank_sigmoid_relevance_unchanged` — GREEN guard
8. `test_recent_mode_passthrough_at_seam` — GREEN guard
9. `test_empty_working_set_no_op` — GREEN guard

At handoff, every bullet above is in state 1 (passing test whose name
transcribes the bullet text verbatim) per the AGENTS.md Closure Rule.
The first commit on the branch shows the RED / guard evidence: the
four RED tests (the original test contract) fail under current
behaviour, the four GREEN guards pass. The follow-up seam-impl and
dedup-fix commits flip the RED to green. A fifth RED bullet
(`test_dedup_equal_score_prefers_lower_plane`, parametrized over
gather order) was added in response to exact-head review feedback;
both parametrizations pass after the dedup fix.

### Discriminating proof

The RED bullet #1 (and its analogues #2, #3) directly proves the
issue's "weak plane's sole hit does not become maximally relevant
merely by being alone." The RED bullet #3 proves the critical
ordering correction: calibrating BEFORE ``best_by_id`` dedup is
required, not after, because a higher-recalibrated copy in a later
target can be discarded by a tie on the per-leg score.

### Test correction (per design ACK 2026-07-15 07:13)

The first contract asserts **global relevance ratio / order and final
rank**, not a final-score margin. Final score is the weighted
combination of five components, so an assertion of "final score
margin >= 0.9" is the wrong contract — it would also fail for the
correct seam output. The contract is on the relevance component and
on the final rank order; the weighted total is a downstream
consequence, not the discriminator.

## Out of scope (NOT closed by this slice)

- The deep and blended relevance paths use the cross-encoder
  ``_sigmoid(rerank_score)``. The seam preserves them unchanged.
  Re-anchoring cross-encoder scores against a working-set max is
  explicitly NOT in scope for this slice.
- Per-plane corpus-level p99 RRF as an alternative global calibration
  source. Working-set max is the chosen contract; a corpus-level
  calibration would be a follow-up.
- The ``scoring.SCORE_WEIGHTS`` defaults. The seam does not touch the
  weights; the contract is intrinsic to the working set.
- Deep reranker result calibration, blended provenance rebalancing,
  per-mode scoring rewrite. All out of scope; the seam is a single
  normalization step.

## Issue #512 assignment path (work-log audit trail)

The GitHub Issue #512 is left **unassigned** in this slice. The
agent-bridge assignee path failed with the GraphQL error
``Could not resolve to a user or bot with the login 'minimax-m3'``
(returned by ``gh issue edit 512 --add-assignee minimax-m3``). The
owner frontmatter on this slice is ``cowork-tama`` per the design
ACK; the GitHub-side assignee is an org-admin / repo-owner action
that is out of scope for the slice work. The Issue label is flipped
to ``status:in-progress`` so the work is visibly claimed, and the
slice frontmatter is the authoritative intent record per the
AGENTS.md Dual-update rule.

This is logged in the Work log below. The Issue assignment is a
follow-up action, not a block on the slice.

## Work log

### 2026-07-15 — cowork-tama (design ACK + slice doc + test contract, no production edits yet)

- **Drift report:** started at worktree HEAD `2637e87`, fast-forwarded
  to live `origin/main` `fc05c7e` (3 commits behind: `7447693` DQ-001,
  `7d1d967` release 1.16.0, `fc05c7e` deploy pin). The seam code paths
  (``scoring._relevance``, per-leg `batch_max_rrf`, multi-target merge)
  are bit-for-bit identical between `2637e87` and `fc05c7e`. Recon
  is faithful.
- **Design ACK (07:13:35):** received from Yua with four binding
  corrections and one critical ordering correction. All four
  pre-fork recommendations accepted as-stated (working-set max,
  sigmoid untouched, object-id tie-break, raw fields on internal
  ``RetrievalResult``). Critical correction: calibrate **before**
  ``best_by_id`` dedup, not after. Test correction: assert global
  relevance ratio / order and final rank, not final-score margin.
- **Test contract:** 9 bullets (10 items; bullet 5 is parametrized over
  gather order: `curated_first`, `episodic_first`); 5 RED
  discriminating tests + 4 GREEN preservation guards. Bounded to
  cross-plane ranking only.
- **Issue claim path:** Issue #512 left unassigned due to GraphQL
  ``replaceActorsForAssignable`` failure on the ``minimax-m3`` login.
  Issue label flipped to ``status:in-progress``; slice frontmatter
  ``owner: cowork-tama`` is the authoritative work-assignment record.
  Assignment reconciliation is an org-admin follow-up.
- **First bounded commit (this branch):** slice doc + spec + claim
  lock + test file (4 files, 763 insertions, zero src). The four
  RED tests fail under current code, the four GREEN guards pass.

### 2026-07-15 — cowork-tama (seam impl: pre-dedup global calibration, deterministic sort key, raw fields)

- **Seam impl commit (this branch, follow-up):** 3 src files + 2 test
  files (238 insertions, 18 deletions). All 8 ret012 tests now
  pass; full suite 2092 passed, 194 skipped, 4 xfailed, zero
  regressions.
- **Files touched:**
  - `src/musubi/retrieve/scoring.py` — added two optional raw fields
    on `ScoredHit` (`raw_rrf_score`, `raw_rerank_score`); propagated
    through `score_result`; added `calibrate_global_relevance` (the
    seam) as a free function with duck-typed input (any object with
    the raw fields and `score_components`). Imported `replace` from
    `dataclasses`.
  - `src/musubi/retrieve/fast.py` — added the two raw fields on
    `FastHit`; populated in `_pack` (`raw_rrf_score=hybrid_hit.score`,
    `raw_rerank_score=None` for fast mode).
  - `src/musubi/retrieve/orchestration.py` — added the two raw fields
    on `RetrievalResult` (internal-only, never on wire models);
    populated at the three leg boundaries (fast branch, deep / blended
    `_pack_scored_hits`, recent branch is default `None` / `None`);
    restructured the multi-target branch in `_retrieve_uncounted` to
    (1) collect every leg's hits into a flat list, (2) call
    `calibrate_global_relevance` on the flat list BEFORE `best_by_id`,
    (3) build `best_by_id` from the calibrated list, (4) sort by
    `(-score, object_id, plane)` for deterministic cross-plane
    ordering. Single-target fast path bit-for-bit preserved.
  - `tests/retrieve/test_ret012_cross_plane_ranking.py` — updated the
    per-leg mocks to populate `raw_rrf_score` (and
    `raw_rerank_score` for the rerank case) on the constructed
    `RetrievalResult`s. The seam needs the raw inputs to recompute
    relevance; without them the seam's passthrough branch fires and
    the RED contracts are not exercised.
  - `tests/retrieve/test_fast.py` — **concrete invariant conflict fix:**
    `test_fast_path_does_not_call_reranker` grepped for the literal
    substring "rerank" in `fast.py`, which now matches the new
    `raw_rerank_score` field name and its docstrings / comments.
    The seam impl does not call the rerank function — it only adds
    the field, set to `None` for fast mode. Tightened the assertion
    to check the actual invariant: no `musubi.retrieve.rerank` import
    and no `run_rerank` call. This is a test correctness fix, not a
    scope expansion.
- **Gates:** `make check` exit 0 (ruff format, ruff check, mypy
  strict, pytest 2092 passed, coverage ≥ 85%); `make tc-coverage
  SLICE=slice-ret012-cross-plane-ranking` exit 0 (8/8 bullets
  passing, ✓ Closure Rule satisfied); `make agent-check` exit 0
  (warnings only, all pre-existing; the only ret012-specific warning
  is the same "no GH Issue titled 'slice: …'" meta-issue gap that
  affects every slice in the repo).
- **Spec drift:** none. The slice doc, the spec
  (`05-retrieval/cross-plane-ranking.md`), and the impl are
  consistent. The helper intentionally uses the duck-typed signature
  `calibrate_global_relevance(candidates: list[Any]) -> list[Any]` to
  avoid importing `RetrievalResult` into `scoring.py`; the orchestration
  caller casts the returned list back to `list[RetrievalResult]`. There
  is no `now` parameter because the seam is purely intrinsic on the
  working set, with no time-dependent logic.
- **No PR open yet.** The seam impl commit is review-ready; the
  draft PR will be opened after this commit, with the body linking
  Issue #512 and the first commit's test evidence.

### 2026-07-15 — cowork-tama (review feedback: equal-score dedup tie-break gap; main@76878c7 integration; handoff hygiene)

- **Review feedback (Yua 08:21 REQ):** a real binding tie-break gap
  remained. The dedup used a strict ``hit.score > current.score``,
  which let the first-seen copy win on equal scores and leaked
  the gather order into the final result. The final
  ``(-score, object_id, plane)`` sort could not repair it because
  one copy was already discarded by the dedup.
- **Fix-forward commits (this branch, follow-up):**
  1. Added `test_dedup_equal_score_prefers_lower_plane` (5th RED
     bullet, parametrized over gather order: `curated_first`,
     `episodic_first`). Discriminates: under the old code the
     ``episodic_first`` parametrization picked the wrong copy
     (gather-order leak); under the fix both parametrizations pick
     the lexicographically smaller plane.
  2. Added `_dedup_prefers(candidate, incumbent)` helper in
     `orchestration.py` that mirrors the final sort key. Higher
     score wins; on tie, lower `object_id` wins; on further tie,
     lower `plane` wins (defense in depth; equal `object_id` is
     impossible after dedup). Replaced the strict
     ``hit.score > current.score`` with the helper.
  3. Updated the slice doc's contract section to bind the dedup
     key to the final sort key (bullet 4 expanded). Test Contract
     section now lists 9 bullets (5 RED + 4 GREEN). Updated the
     spec `05-retrieval/cross-plane-ranking.md` to match.
  4. Integrated `main@76878c7` via `git merge origin/main` (no
     rebase; fix-forward preserves the existing tests-first
     history). RET-002 (orchestration accounting) is structurally
     disjoint from RET-012 (cross-plane relevance calibration) and
     the merge had no conflicts.
  5. Deleted `docs/Musubi/_inbox/locks/slice-ret012-cross-plane-ranking.lock`
     on handoff (per the slice lifecycle: the lock marks the
     in-progress claim and is removed when the slice goes to
     in-review; the slice frontmatter `status: in-review` is the
     authoritative state record per AGENTS.md Dual-update rule).
  6. Edited the PR body to remove the "draft PR" sentence (PR #521
     was marked ready for review in the previous round; the body
     still said "this is a draft PR" — fix-forward cleanup).
- **Files touched in the fix-forward commits:**
  `src/musubi/retrieve/orchestration.py` (dedup helper + call site);
  `tests/retrieve/test_ret012_cross_plane_ranking.py` (5th RED
  bullet, parametrized);
  `docs/Musubi/_slices/slice-ret012-cross-plane-ranking.md` (contract
  bullet 4 expanded, Test Contract 9 bullets, work log);
  `docs/Musubi/05-retrieval/cross-plane-ranking.md` (Test Contract 9
  bullets); plus the lock delete and the PR body edit.
- **Test count:** 10 items (9 bullets; bullet 5 is parametrized).
  All 10 ✓ passing.
- **No spec drift:** the function signature is unchanged. The new
  behavior is a tightening of the dedup key, not a new function or
  a new field.

## Out-of-band continuation

- **Cross-encoder calibration:** out of scope per Design ACK; a
  follow-up slice could revisit whether the working-set max should
  also re-anchor the cross-encoder sigmoid when the candidate set is
  sparse (a sole reranked hit). Not the contract of this slice.
- **Corpus-level p99 RRF calibration:** explicitly out of scope; a
  follow-up could add a per-`(ns, plane)` p99 stored on the plane
  payload. Working-set max is the chosen contract here.
- **GitHub Issue #512 assignee resolution:** org-admin / repo-owner
  follow-up. The slice's owner frontmatter (``cowork-tama``) is the
  authoritative work-assignment record until that is reconciled.
