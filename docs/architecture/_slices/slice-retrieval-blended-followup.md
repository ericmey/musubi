---
title: "Slice: Blended retrieval — Test Contract followup"
slice_id: slice-retrieval-blended-followup
section: _slices
type: slice
status: ready
owner: unassigned
phase: "5 Retrieval"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-retrieval-blended]]"]
blocks: []
---

# Slice: Blended retrieval — Test Contract followup

> Behavioral-correctness test coverage for `src/musubi/retrieve/blended.py`.
> Implementation landed in slice-retrieval-blended (PR #79); this slice
> implements the 13 Test Contract bullets deferred there under time pressure.

**Phase:** 5 Retrieval · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[05-retrieval/blended]]  (the 13 bullets marked `@pytest.mark.skip(reason="deferred to slice-retrieval-blended-followup: ...")` in tests/retrieve/test_blended.py on v2)

## Owned paths (you MAY write here)

- `tests/retrieve/test_blended.py`  (un-skip the 13 deferred tests and implement them against real blended.py behaviour)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/retrieve/blended.py`  (implementation already landed; behavioral-change requires slice-retrieval-blended-v2)
- `src/musubi/retrieve/{hybrid,scoring,rerank,fast,deep}.py`
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-retrieval-blended]] (must be `status: done` before this slice can start)

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- _(none yet)_

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every skipped test in tests/retrieve/test_blended.py is un-skipped AND passing.
- [ ] Branch coverage ≥ 90 % on `src/musubi/retrieve/blended.py` (retrieval floor).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 — gemini-2-0-flash — slice created

- Created as follow-up to slice-retrieval-blended (PR #79). 13 Test Contract
  bullets from 05-retrieval/blended.md were deferred during the parent slice's
  implementation due to time pressure; all had skip-reason pointing at this
  slice. This slice legitimizes that pointer.
