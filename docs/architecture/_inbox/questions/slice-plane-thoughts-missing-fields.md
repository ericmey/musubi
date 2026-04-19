---
title: "Question: Missing lineage fields in Thought"
section: _inbox
type: question
status: proposed
tags: [section/inbox, status/proposed, type/question]
updated: 2026-04-19
---

# Question: Missing lineage fields in Thought

**Goal:** Implement `ThoughtsPlane.history` with `in_reply_to` and satisfy `test_thought_in_reply_to_chain_queries_correctly`.

**Expectation:** The `Thought` model in `src/musubi/types/thought.py` has `in_reply_to` and `supersedes` fields as documented in `docs/architecture/04-data-model/thoughts.md`.

**Observation:** `Thought` inherits from `MusubiObject` and does not define `in_reply_to` or `supersedes`. Because my agent guardrails forbid modifications outside of `src/musubi/planes/thoughts/` (specifically I cannot edit `src/musubi/types/`), I cannot resolve this directly.

**Options:**
1. A human or the `slice-types` agent updates `Thought` to include these fields. I have filed a cross-slice ticket at `docs/architecture/_inbox/cross-slice/slice-plane-thoughts-slice-types-missing-lineage-fields.md`.
2. Amend the `thoughts.md` spec to defer lineage features to a later phase (if this omission in POC was intentional).
