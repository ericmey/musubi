---
title: "Slice: Ansible deployment"
slice_id: slice-ops-ansible
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: []
blocks: ["[[_slices/slice-ops-backup]]", "[[_slices/slice-ops-compose]]", "[[_slices/slice-ops-observability]]"]
---
# Slice: Ansible deployment

> Playbook stands up fresh Debian host: Qdrant + Core + Lifecycle Worker + vault bind-mount + Kong.

**Phase:** 8 Ops · **Status:** `in-progress` · **Owner:** `codex-gpt5`

## Specs to implement

- [[08-deployment/ansible-layout]]
- [[08-deployment/host-profile]]

## Owned paths (you MAY write here)

  - `deploy/ansible/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/`

## Depends on

  - _(no upstream slices)_

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-ops-observability]]
  - [[_slices/slice-ops-backup]]

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

### 2026-04-19 18:45 — codex-gpt5 — claimed slice

- Claimed Issue #16 and flipped slice frontmatter from `ready` to `in-progress`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
