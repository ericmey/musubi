---
title: "Slice: LIFE-009 semantic supersession with abstention"
slice_id: slice-life009-semantic-supersession
issue: 532
section: _slices
type: slice
status: in-review
owner: cowork-tama
phase: "Lifecycle"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---

# Slice: LIFE-009 semantic supersession with abstention

## What

Closes the semantic-supersession gap (Issue #532). The current
``_find_supersession_candidate`` in
``src/musubi/lifecycle/maturation.py`` uses bounded substring
containment to find a candidate predecessor. Corrections can miss
their predecessor, unrelated content can link incorrectly, and
ambiguous cases do not abstain reliably.

This slice replaces the substring-based heuristic with a semantic
similarity + topic-compatibility check using the existing embedding
and topic infrastructure. The spec
(``docs/Musubi/06-ingestion/maturation.md`` § Step 5) already calls
for similarity ≥ 0.88 plus the same topic; the current code defers
to a "follow-up" with a substring placeholder. The slice makes
the spec real.

The slice is bounded to:

- ``src/musubi/lifecycle/maturation.py`` — replace substring
  containment with semantic similarity + topic match in
  ``_find_supersession_candidate``; add bounded candidate search.
- ``tests/lifecycle/test_life009_semantic_supersession.py`` — 13
  tests covering the adversarial corpus.
- (No new subsystem; no new module; the embedder and topic
  infrastructure already exist.)

## Why

Per Issue #532:
- "Supersession promises semantic similarity plus topic
  compatibility but uses bounded substring containment."
- "Only a factually and topically compatible successor may
  supersede a predecessor. Ambiguity must abstain; correction
  and negation semantics must be explicit."

The current ``_find_supersession_candidate`` (maturation.py:822)
compares candidates via ``needle in candidate_content`` or
``candidate_content in needle``. This is exactly the bug: a
correction like "Correction: the meeting is at 3pm" must link to
"the meeting is at 2pm" by semantic similarity (the needle
"the meeting is at 3pm" is not a substring of the candidate
"the meeting is at 2pm"); an unrelated row that shares a substring
can be incorrectly linked; multiple plausible candidates cannot
abstain.

## Contract

1. **Semantic similarity check.** The new
   ``_find_supersession_candidate`` embeds the post-hint needle
   using the existing ``Embedder`` (the slice is not a new
   subsystem; it uses the same ``_TEICompositeEmbedder`` the
   maturation runner already wires). The similarity to each
   candidate's content (post-hint) is computed via cosine
   similarity. A candidate passes only if similarity ≥ 0.88.

2. **Topic compatibility check.** A candidate passes only if it
   shares at least one ``linked_to_topics`` entry with the new
   memory's topics. Two memories on different topics must not
   supersede each other even if their text is similar (e.g.,
   "the battery is at 3pm" vs "the meeting is at 3pm" — high
   text similarity, different topics, ABSTAIN).

3. **Correction and negation semantics.** The "needle" used for
   similarity is the post-hint content (e.g., "Correction: the
   meeting is at 3pm" → needle "the meeting is at 3pm"). The
   candidate's content is also post-hint stripped. The predecessor
   is the row whose content is the correction/negation target —
   i.e., the candidate whose content is semantically the
   negation of the needle. The semantic similarity identifies
   the right link (no special-case logic for "correction:" vs
   "negation:" vs "update:" — the same heuristic works for all).

4. **Abstention on ambiguity.** If zero candidates pass, return
   ``None`` (no supersession inferred). If two or more candidates
   pass, return ``None`` (abstain on ambiguity — per the
   invariant: "ambiguous candidates abstain"). Only when
   exactly one candidate passes is the supersession inferred.

5. **Bounded candidate search.** The new function takes a
   ``max_candidates`` argument (default 20) and passes it to the
   Qdrant ``scroll(limit=...)`` call. The current code uses
   ``limit=50``; the new code is bounded to 20 to keep the
   per-row cost predictable.

6. **Predecessor and back-link correctness.** When the
   supersession is inferred, the existing maturation sweep
   (``episodic_maturation_sweep``) sets the predecessor's
   ``superseded_by`` and the new memory's ``supersedes`` (this
   path is already exercised by the existing
   ``test_supersession_sets_both_sides_of_link`` test; the slice
   preserves the contract).

7. **Retry / idempotency.** Running the maturation sweep twice
   with the same input produces the same supersession decision.
   The existing
   ``test_supersession_no_predecessor_match`` test exercises
   the no-predecessor branch; the slice adds a retry test
   for the with-predecessor branch.

8. **No new subsystem.** The seam uses the existing
   ``Embedder`` (passed as an argument) and the existing
   ``OllamaClient.infer_topics`` (used to compute the new
   memory's topics). No new file, no new module, no new
   configuration.

## Spec drift

The existing spec at
``docs/Musubi/06-ingestion/maturation.md`` § Step 5 already
describes the semantic + topic invariant:

> "we check for a previous memory in the same namespace with
> high semantic similarity (≥ 0.88) and the same topic. If
> found: set supersedes: [old_id] and superseded_by: new_id. If
> not found, we don't infer supersession — it's a conservative
> step."

The slice makes this real. No spec change is needed; the
slice's work log records the implementation.

## Specs to implement

- [[06-ingestion/maturation#Step 5 — Optional supersession detection]] (spec calls for similarity ≥ 0.88 plus same topic; current code uses bounded substring containment, which the slice replaces)

## Acceptance

The first contract is bounded to sixteen tests in
``tests/lifecycle/test_life009_semantic_supersession.py``:
eleven RED discriminating tests, two GREEN preservation
guards, one RED→GREEN call-shape discriminator, two pre-existing
test-maturation migrations (controlled embedder + shared
``linked_to_topics`` evidence). Test function names transcribe the
Test Contract bullets verbatim per the AGENTS.md Test Contract
Closure Rule.

### Test Contract (16 bullets, state 1 = passing at handoff)

1. `test_paraphrase_supersession` — RED
2. `test_correction_supersession_links_to_right_predecessor` — RED
3. `test_negation_supersession_links_to_right_predecessor` — RED
4. `test_participant_change_supersession` — RED
5. `test_time_change_supersession` — RED
6. `test_unrelated_substring_overlap_does_not_supersede` — RED
7. `test_ambiguous_candidates_abstain` — RED
8. `test_no_candidates_abstain` — RED
9. `test_threshold_below_minimum_abstains` — RED
10. `test_predecessor_and_back_link_correctness_in_sweep` — RED
11. `test_retry_idempotency_in_sweep` — RED
12. `test_bounded_candidate_search` — RED
13. `test_substring_only_does_not_match` — RED (the OLD substring
    logic would have matched these; the NEW semantic logic does
    not)
14. `test_seam_makes_exactly_one_embed_dense_call` — RED→GREEN call-shape discriminator (close-out add: seam makes exactly one batched `embed_dense` call, regardless of candidate count)

GREEN preservation guards (the seam must not break the existing
correctness contract; the slice adds GREEN guards that pin the
existing behavior):

15. `test_existing_no_predecessor_branch_still_returns_none` — GREEN
16. `test_existing_both_sides_of_link_still_set` — GREEN

15 tests total. The first commit (slice doc + spec + lock + test
file) is tests-only; the seam impl is a follow-up commit.

## Issue #532 assignment path (work-log audit trail)

The GitHub Issue #532 is left **unassigned** in this slice. The
agent-bridge assignee path failed with the GraphQL error
``Could not resolve to a user or bot with the login 'minimax-m3'``
on ``gh issue edit 532 --add-assignee minimax-m3`` (the same
pre-existing failure that affected Issues #512 and #523). The
owner frontmatter on this slice is ``cowork-tama`` per the
design pattern established by the upstream slices; the
GitHub-side assignee is an org-admin / repo-owner action that is
out of scope for the slice work. The Issue label is flipped to
``status:in-progress`` so the work is visibly claimed.

This is logged in the Work log below. The Issue assignment is a
follow-up action, not a block on the slice.

## Work log

### 2026-07-15 — cowork-tama (inspect + slice doc + test contract)

- **Drift / inspect.** Started at worktree HEAD ``815ce45``
  (origin/main, no drift on this lane). New worktree
  ``/tmp/life009/wt`` on branch
  ``slice/life009-semantic-supersession``.
- **Inspect (supersession seam).** The current
  ``_find_supersession_candidate`` in
  ``src/musubi/lifecycle/maturation.py:822`` uses substring
  containment: ``candidate_content == needle`` OR
  ``needle in candidate_content`` OR
  ``candidate_content in needle``. The docstring already
  acknowledges the spec wants "similarity ≥ 0.88 plus topic
  match" but defers to a follow-up. The slice makes the spec
  real.
- **Inspect (existing infrastructure).** The maturation runner
  already wires ``_TEICompositeEmbedder`` (dense + sparse +
  reranker). The slice does not add a new subsystem; it
  uses the existing ``Embedder`` protocol as a function
  argument and the existing ``OllamaClient.infer_topics`` for
  the new memory's topics.
- **Inspect (LIFE-005 / #516 ret012 cross-plane seam).** The
  ret012 cross-plane ranking seam ships on
  ``slice/ret-012-cross-plane-ranking`` (PR #521 ready, awaiting
  Eric's independent review per "no self-merge"). No shared
  infrastructure conflict; the LIFE-009 seam operates in
  ``src/musubi/lifecycle/`` and the ret012 seam operates in
  ``src/musubi/retrieve/``.
- **Issue claim path.** Issue #532 label flipped to
  ``status:in-progress``; assignee add failed with the same
  ``minimax-m3`` GraphQL error; logged as a non-blocking
  open-defect in the slice doc.
- **Test contract.** 15 tests, bounded per AGENTS.md Closure
  Rule: 11 RED discriminating + 2 GREEN preservation + 2 RED
  structural.

## Out of scope (NOT closed by this slice)

- The semantic similarity threshold (0.88) is a spec value; the
  slice does NOT tune it. A future slice may tune based on a
  measured corpus result with explicit review.
- The bounded candidate search uses a default of 20 candidates;
  the slice does NOT tune this either.
- The "Correction:" / "Negation:" / "Update:" hints are detected
  as a content prefix; the slice does NOT add new hint types or
  change the prefix detection.
- The slice does NOT touch the cross-plane ranking seam (RET-012)
  or the per-agent exclusion seam (AUTH-001).
- The slice does NOT add a new subsystem (no new file, no new
  module, no new configuration).
