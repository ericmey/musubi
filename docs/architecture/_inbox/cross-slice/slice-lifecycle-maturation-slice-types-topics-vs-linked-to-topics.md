---
title: "Cross-slice: EpisodicMemory.topics field name vs linked_to_topics"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-lifecycle-maturation
target_slice: slice-types
status: open
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/open]
updated: 2026-04-19
---

# Reconcile `topics` (spec) vs `linked_to_topics` (model) on `EpisodicMemory`

## Source slice

`slice-lifecycle-maturation` (PR #52).

## Problem

`docs/architecture/06-ingestion/maturation.md` § Step 4 — Topic inference
calls for the maturation sweep to write inferred topics back to the
episodic memory:

> Output JSON: `{"id": <ksuid>, "topics": [<topic>, ...]}`

The spec uses the field name **`topics`**. The pydantic model in
`src/musubi/types/episodic.py` (via `MemoryObject` in
`src/musubi/types/base.py`) declares only **`linked_to_topics`**. There
is no `topics` field on `EpisodicMemory`. `topics` exists on
`CuratedKnowledge` only (overridden in `src/musubi/types/curated.py`).

`MusubiObject.model_config` is `extra="forbid"`, so any maturation write
attempting to populate `topics` on an episodic row would fail with
`ValidationError`. The Qdrant payload index in
`src/musubi/store/specs.py::UNIVERSAL_INDEXES` uses `topics` (lifted to
all collections), which compounds the inconsistency: the index expects a
field the episodic model does not provide.

## Impact on slice-lifecycle-maturation

- The spec's "topic inference" output is currently written to
  `linked_to_topics` (the field that exists). Tests assert against
  `linked_to_topics` to match the implementation.
- Bullet 9 (`test_topics_inferred_from_llm`) and bullet 10
  (`test_topics_empty_on_unknown`) pass against `linked_to_topics`, not
  `topics`.
- Future retrieval slices that read "topics" off an episodic row would
  hit an empty field.

## What this slice did instead

Wrote the LLM-inferred topics to `linked_to_topics`. Documented the
choice in the maturation module docstring + slice work-log.

## Requested change

Pick one of the two reconciliations and apply it consistently across
the type, the spec, and the indexes:

**Option A (recommended) — add `topics` to `EpisodicMemory`.** Match the
spec by giving the episodic model its own `topics: list[str]` field
(separate from `linked_to_topics`, which carries cross-references rather
than first-class topic membership):

```python
# src/musubi/types/episodic.py
topics: list[str] = Field(default_factory=list)
```

This matches `CuratedKnowledge.topics` semantics and lets the
`UNIVERSAL_INDEXES` `topics` keyword index work uniformly across
collections.

**Option B — update the spec to say `linked_to_topics`.** Smaller
change; preserves today's behaviour but requires a coordinated
`spec-update:` to `06-ingestion/maturation.md` and removes the `topics`
universal index for episodic.

If Option A: this slice's enrichment write switches from
`linked_to_topics` to `topics` in a follow-up PR; the maturation tests
update accordingly.

## Acceptance

- One of A / B is chosen and applied; spec, model, and store/specs.py
  all agree on a single field name for "topics on an episodic memory".
- `slice-lifecycle-maturation` follow-up updates
  `_apply_enrichment` + the two passing topic tests to match.
- `make agent-check` reports no spec drift on the relevant fields.
