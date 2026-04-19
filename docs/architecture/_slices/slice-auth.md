---
title: "Slice: Auth middleware"
slice_id: slice-auth
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "1 Schema"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-config]]"]
blocks: ["[[_slices/slice-api-v0]]"]
---

# Slice: Auth middleware

> Bearer token validation + namespace scope check + optional mTLS. Sits as middleware; business logic never parses auth headers.

**Phase:** 1 Schema · **Status:** `in-progress` · **Owner:** `codex-gpt5`

## Specs to implement

- [[10-security/auth]]

## Owned paths (you MAY write here)

- `musubi/auth/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/api/routes/`
- `musubi/planes/`

## Depends on

- [[_slices/slice-config]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-api-v0]]

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

### 2026-04-19 10:38 — codex-gpt5 — claim

- Claimed slice via Issue #7 and branch `slice/slice-auth`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
