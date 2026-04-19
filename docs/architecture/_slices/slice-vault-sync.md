---
title: "Slice: Obsidian vault watcher + reconciler"
slice_id: slice-vault-sync
section: _slices
type: slice
status: in-progress
owner: gemini-3-1-pro-nyla
phase: "5 Vault"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-plane-curated]]", "[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-lifecycle-reflection]]"]
---

# Slice: Obsidian vault watcher + reconciler

**Phase:** 5 Vault · **Status:** `in-progress` · **Owner:** `gemini-3-1-pro-nyla`

## Specs to implement
...
## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 15:00 — gemini-3-1-pro-nyla — claim

- Claimed slice via `pick-slice` skill. Issue #35, PR #64 (draft).

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

- [[06-ingestion/vault-sync]]
- [[06-ingestion/vault-frontmatter-schema]]

## Owned paths (you MAY write here)

  - `musubi/vault_sync/`
  - `tests/vault/test_sync.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/planes/`
  - `musubi/api/`

## Depends on

  - [[_slices/slice-plane-curated]]
  - [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-lifecycle-promotion]]
  - [[_slices/slice-lifecycle-reflection]]

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
