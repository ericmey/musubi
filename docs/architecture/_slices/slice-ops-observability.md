---
title: "Slice: Metrics / logs / traces"
slice_id: slice-ops-observability
section: _slices
type: slice
status: ready
owner: unassigned
phase: "8 Ops"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-ops-compose]]"]
blocks: ["[[_slices/slice-ops-backup]]"]
---

# Slice: Metrics / logs / traces

> Structured logs (Loki), metrics (Prometheus), traces (Tempo). Alert rules and dashboards ship in repo.

**Phase:** 8 Ops · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[09-operations/observability]]
- [[09-operations/alerts]]

## Owned paths (you MAY write here)

  - `musubi/observability/`
  - `deploy/grafana/`
  - `deploy/loki/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/planes/`
  - `musubi/api/`

## Depends on

  - [[_slices/slice-ops-compose]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

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

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
