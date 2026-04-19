---
title: "Slice: Python SDK"
slice_id: slice-sdk-py
section: _slices
type: slice
status: ready
owner: unassigned
phase: "5 Vault"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-api-v0-write]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]"]
---
# Slice: Python SDK

> Thin HTTP + gRPC client. Handles auth, retries, typed errors. Separate repo; pinned to API version.

**Phase:** 5 Vault · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[07-interfaces/sdk]]

## Owned paths (you MAY write here)

- `musubi-sdk-py/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/`

## Depends on

- [[_slices/slice-api-v0-write]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-adapter-mcp]]
- [[_slices/slice-adapter-livekit]]

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
