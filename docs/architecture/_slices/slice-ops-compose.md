---
title: "Slice: Docker Compose stack"
slice_id: slice-ops-compose
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-ops-ansible]]"]
blocks: ["[[_slices/slice-ops-observability]]"]
---

# Slice: Docker Compose stack

> Compose file covering Qdrant, Core, Lifecycle, TEI, Ollama, Kong. Health checks + startup order.

**Phase:** 8 Ops · **Status:** `in-progress` · **Owner:** `codex-gpt5`

## Specs to implement

- [[08-deployment/compose-stack]]
- [[08-deployment/kong]]

## Owned paths (you MAY write here)

  - `deploy/docker/`
  - `docker-compose.yml`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/`

## Depends on

  - [[_slices/slice-ops-ansible]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-ops-observability]]

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

### 2026-04-19 19:36 — codex-gpt5 — claimed slice

- Claimed Issue #18 and flipped slice frontmatter from `ready` to `in-progress`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
