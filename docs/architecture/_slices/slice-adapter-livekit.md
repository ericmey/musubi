---
title: "Slice: LiveKit adapter"
slice_id: slice-adapter-livekit
section: _slices
type: slice
status: ready
owner: unassigned
phase: "6 Lifecycle"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-sdk-py]]", "[[_slices/slice-retrieval-fast]]"]
blocks: []
---

# Slice: LiveKit adapter

> LiveKit Agents toolkit: Fast Talker + Slow Thinker pattern. `on_user_turn_completed` hook. Hard 200ms budget.

**Phase:** 6 Lifecycle · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[07-interfaces/livekit-adapter]]

## Owned paths (you MAY write here)

- `musubi-livekit/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/`
- `musubi-sdk-py/`

## Depends on

- [[_slices/slice-sdk-py]]
- [[_slices/slice-retrieval-fast]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- _(no downstream slices)_

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
