---
title: "Slice: Thoughts subsystem"
slice_id: slice-plane-thoughts
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-nyla
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-adapter-mcp]]"]
---

# Slice: Thoughts subsystem

> POC-preserved inter-presence durable message channel. Per-presence read-state via `read_by` list.

**Phase:** 4 Planes · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[04-data-model/thoughts]]

## Owned paths (you MAY write here)

- `musubi/thoughts/`
- `tests/test_thoughts.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`
- `musubi/api/`

## Depends on

- [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-adapter-mcp]]

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

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
