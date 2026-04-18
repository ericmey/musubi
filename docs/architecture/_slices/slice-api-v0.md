---
title: "Slice: Canonical API v0.1"
slice_id: slice-api-v0
section: _slices
type: slice
status: ready
owner: unassigned
phase: "7 Adapters"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-config]]", "[[_slices/slice-auth]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-sdk-py]]"]
---
# Slice: Canonical API v0.1

> The single HTTP + gRPC surface. Frozen per version; new endpoints require ADR. Blocks every adapter.

**Phase:** 7 Adapters · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[07-interfaces/canonical-api]]
- [[07-interfaces/contract-tests]]

## Owned paths (you MAY write here)

  - `musubi/api/`
  - `openapi.yaml`
  - `proto/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/planes/`
  - `musubi/retrieve/`
  - `musubi/lifecycle/`

## Depends on

  - [[_slices/slice-types]]
  - [[_slices/slice-config]]
  - [[_slices/slice-auth]]
  - [[_slices/slice-plane-episodic]]
  - [[_slices/slice-plane-curated]]
  - [[_slices/slice-plane-artifact]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-sdk-py]]
  - [[_slices/slice-adapter-mcp]]
  - [[_slices/slice-adapter-livekit]]
  - [[_slices/slice-adapter-openclaw]]

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
