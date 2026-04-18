---
title: "Slice: Config & environment loading"
slice_id: slice-config
section: _slices
type: slice
status: ready
owner: unassigned
phase: "1 Schema"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: []
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-auth]]", "[[_slices/slice-embedding]]"]
---
# Slice: Config & environment loading

> Single source of truth for environment variables. All config reads go through one module; agents must not read os.environ directly elsewhere.

**Phase:** 1 Schema · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[00-index/conventions]]

## Owned paths (you MAY write here)

- `musubi/config.py`
- `musubi/settings.py`
- `.env.example`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/api/`
- `musubi/planes/`

## Depends on

- _(no upstream slices)_

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-api-v0]]
- [[_slices/slice-auth]]

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
