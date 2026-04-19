---
title: "Cross-slice: add lineage fields to Thought type"
section: _inbox
type: cross-slice
status: proposed
tags: [section/inbox, status/proposed, type/cross-slice]
updated: 2026-04-19
depends-on: ["[[_slices/slice-types]]"]
---

# Cross-slice: add lineage fields to Thought type

**Source slice:** `slice-plane-thoughts`
**Target slice:** `slice-types`

## Problem

The spec for Thoughts (`docs/architecture/04-data-model/thoughts.md`) specifies two lineage fields:
```python
    in_reply_to: KSUID | None = None
    supersedes: list[KSUID] = Field(default_factory=list)
```

However, the implementation of `Thought` in `src/musubi/types/thought.py` inherits from `MusubiObject` (not `MemoryObject`) and lacks these fields. 

Because `slice-plane-thoughts` cannot mutate `src/musubi/types/`, this blocks test `test_thought_in_reply_to_chain_queries_correctly` from being written and implemented properly.

## Action required

`slice-types` owner needs to add `in_reply_to: KSUID | None = None` and `supersedes: list[KSUID] = Field(default_factory=list)` to the `Thought` pydantic model in `src/musubi/types/thought.py`.
