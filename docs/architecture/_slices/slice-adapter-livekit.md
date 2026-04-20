---
title: "Slice: LiveKit adapter"
slice_id: slice-adapter-livekit
section: _slices
type: slice
status: ready
owner: unassigned
phase: "6 Lifecycle"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-sdk-py]]", "[[_slices/slice-retrieval-fast]]", "[[_slices/slice-retrieval-deep]]"]
blocks: []
---

# Slice: LiveKit adapter

> LiveKit Agents toolkit: Fast Talker + Slow Thinker pattern. `on_user_turn_completed` hook. Hard 200ms budget.

**Phase:** 6 Lifecycle · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[07-interfaces/livekit-adapter]]

## Owned paths (you MAY write here)

- `src/musubi/adapters/livekit/`
- `tests/adapters/test_livekit.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/sdk/`         (owned by slice-sdk-py, done — CALL, don't modify)
- `src/musubi/adapters/mcp/` (owned by slice-adapter-mcp, may be in-progress)
- `src/musubi/adapters/openclaw/` (reserved for slice-adapter-openclaw)
- `src/musubi/retrieve/`    (owned by retrieval DAG, all done — CALL via the SDK)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`
- `src/musubi/ingestion/`
- `src/musubi/lifecycle/`
- `openapi.yaml`
- `proto/`

> **Spec drift note (reviewer convention):** [[07-interfaces/livekit-adapter]]
> opens with "Independent project. Repo: musubi-livekit-adapter. Embedded into
> the LiveKit agent worker as a Python package." Per ADR-0015, the adapter
> lives in-monorepo at `src/musubi/adapters/livekit/` and imports as
> `musubi.adapters.livekit`. Update the spec in-PR with a
> `spec-update: docs/architecture/07-interfaces/livekit-adapter.md` commit
> trailer — same pattern VS Code used on slice-sdk-py.

## Depends on

- [[_slices/slice-sdk-py]] (done — wraps every Musubi call)
- [[_slices/slice-retrieval-fast]] (done — Fast Talker path, ~150ms budget)
- [[_slices/slice-retrieval-deep]] (done — Slow Thinker path, ~2s budget)

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

### 2026-04-19 — operator — reconcile paths to post-ADR-0015 monorepo layout

- 8th pre-src-monorepo drift fix. `owns_paths` was `musubi-livekit/` (pre-monorepo external-repo layout); reconciled to `src/musubi/adapters/livekit/` + `tests/adapters/test_livekit.py` per ADR-0015 §Decision.
- `forbidden_paths` expanded from `musubi/` + `musubi-sdk-py/` to the full post-monorepo list, including sibling adapters so the three-way concurrent-adapter scenario doesn't collide.
- Added `[[_slices/slice-retrieval-deep]]` to `depends-on` (Slow Thinker uses deep-path — was implicitly required but not declared).
- Spec [[07-interfaces/livekit-adapter]] still references the external-repo layout (`musubi-livekit-adapter`); implementing agent updates the spec in-PR with a `spec-update:` trailer.

### 2026-04-19 — vscode-cc-sonnet47 — take

- Claimed atomically via `gh issue edit 3 --add-assignee @me` + label flip `status:ready → status:in-progress` (dual-update before writes, post-#93 drift is now `✗` not `⚠`).
- Branch `slice/slice-adapter-livekit` off `v2`.
- Same agent that landed slice-sdk-py (#90) — the SDK surface is fresh context, so the Fast Talker + Slow Thinker wiring against `AsyncMusubiClient` lands without re-reading the SDK.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
