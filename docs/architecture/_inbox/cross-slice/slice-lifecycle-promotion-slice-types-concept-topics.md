---
title: "Add `topics: list[str]` to `SynthesizedConcept`"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-lifecycle-promotion
target_slice: slice-types
status: resolved
opened_by: gemini-3-1-pro-nyla
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

# Add `topics: list[str]` to `SynthesizedConcept`

## Source slice

`slice-lifecycle-promotion` (PR #68).

## Problem

The spec at `docs/architecture/06-ingestion/promotion.md` declares that `compute_path` uses `concept.topics[0]` to determine the directory for the generated markdown file. However, `SynthesizedConcept` in `src/musubi/types/concept.py` (via `MemoryObject`) only has `linked_to_topics: list[str]`. The spec expects `SynthesizedConcept` to have a `topics` field, but `topics` currently only exists on `CuratedKnowledge`.

## Impact on slice-lifecycle-promotion

- The `compute_path` function cannot access `concept.topics`, resulting in a `ValidationError` or `AttributeError`.
- Test Contract bullet 12 (`test_path_derived_from_topic_and_title`) is blocked and must be skipped.

## What this slice did instead

Skipped the relevant test and used `concept.linked_to_topics` as a fallback implementation for `compute_path` and `topics` transfer so that promotion can at least function. 

## Requested change

Add to `src/musubi/types/concept.py`:

```python
topics: list[str] = Field(default_factory=list)
```

## Acceptance

- `SynthesizedConcept(..., topics=["a/b"])` validates.
- Follow-up updates `slice-lifecycle-promotion` to use `concept.topics` instead of the `linked_to_topics` fallback.

## Resolution

Resolved by PR #113 (`slice-types-followup`).
