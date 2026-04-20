---
title: "Slice: Python SDK"
slice_id: slice-sdk-py
section: _slices
type: slice
status: in-progress
owner: vscode-cc-sonnet47
phase: "5 Vault"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-api-v0-write]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]"]
---
# Slice: Python SDK

> Thin HTTP + gRPC client. Handles auth, retries, typed errors. Separate repo; pinned to API version.

**Phase:** 5 Vault · **Status:** `in-progress` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[07-interfaces/sdk]]

## Owned paths (you MAY write here)

- `src/musubi/sdk/`
- `tests/sdk/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/api/`   (canonical API surface; frozen per version — see ADR-0011, ADR-0015)
- `src/musubi/types/` (owned by slice-types, done)
- `src/musubi/planes/`
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/adapters/`
- `openapi.yaml`
- `proto/`

> **Spec drift note (reviewer convention):** [[07-interfaces/sdk]] predates ADR-0015
> and still describes the SDK as a sibling package `musubi-client/musubi_client/`.
> ADR-0015 / ADR-0016 move the SDK to `src/musubi/sdk/` inside the monorepo
> (importable as `musubi.sdk`). Update the spec in-PR with a
> `spec-update: docs/architecture/07-interfaces/sdk.md` commit trailer — rename
> `musubi-client` → `musubi.sdk`, keep everything else. No canonical-API changes.

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

### 2026-04-19 — operator — reconcile paths to post-ADR-0015 monorepo layout

- `owns_paths` was `musubi-sdk-py/` (pre-monorepo drift); reconciled to
  `src/musubi/sdk/` + `tests/sdk/` per ADR-0015 §Decision.
- `forbidden_paths` expanded from `musubi/` to the full post-monorepo list
  (api/, types/, planes/, retrieve/, lifecycle/, ingestion/, adapters/,
  openapi.yaml, proto/).
- Spec [[07-interfaces/sdk]] still describes `musubi-client` package naming;
  the implementing agent updates the spec in-PR with a `spec-update:` trailer
  per the non-negotiables in CLAUDE.md rule 4.

### 2026-04-19 — vscode-cc-sonnet47 — claim

- Claimed slice atomically via `gh issue edit 33 --add-assignee @me`. Issue #33, PR #90 (draft).
- Branch `slice/slice-sdk-py` off `v2`.
- Verified slice file already canonical (operator pre-reconciled tonight); same agent that landed slice-api-v0-{read,write} + slice-ingestion-capture is closing the SDK loop on top of its own scaffolding.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
