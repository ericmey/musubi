---
title: "Slice: Python SDK"
slice_id: slice-sdk-py
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "5 Vault"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-api-v0-write]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]"]
---
# Slice: Python SDK

> Thin HTTP + gRPC client. Handles auth, retries, typed errors. Separate repo; pinned to API version.

**Phase:** 5 Vault · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

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

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Implemented `src/musubi/sdk/` with sync (`MusubiClient`) + async (`AsyncMusubiClient`) variants, typed exception hierarchy, `RetryPolicy` honouring `Retry-After`, `SDKResult[T]` wrapper, and `FakeMusubiClient` for adapter tests.
- 53 unit tests (20 of 22 contract bullets passing; 1 skipped with cross-slice ticket; 1 declared out-of-scope per work log). Branch coverage on owned code: 93% (gate 85%).
- Spec rename `musubi-client` → `musubi.sdk` (ADR-0015 / ADR-0016) applied in same PR with `spec-update: docs/architecture/07-interfaces/sdk.md` trailer on the feat commit.
- One cross-slice ticket opened: `slice-sdk-py-otel-spans.md` (OpenTelemetry span emission, deferred — opentelemetry-api isn't in dev extras and the spec scopes OTel as opt-in).
- Handoff checks: `make check` green, `make tc-coverage SLICE=slice-sdk-py` reports closure satisfied, `make agent-check` clean (warnings only, none touching this slice), `gh pr view 90 → mergeStateStatus=CLEAN, mergeable=MERGEABLE`, both PR checks (vault hygiene + check) pass.
- Flipping `status: in-review`, marking PR ready, removing the lock.

### Known gaps at in-review

- **OTel emission deferred.** Bullet 16 is skipped against `slice-sdk-py-otel-spans.md`. Adapters that need OTel today should instrument at the adapter layer until the cross-slice lands.
- **No gRPC client.** The spec mentions an optional `[grpc]` extra; this PR ships HTTP only. gRPC is intentionally a follow-up.
- **No models module.** The spec's old `models.py` step ("re-exports from musubi-core.types") is unnecessary in the monorepo — adapters import directly from `musubi.types.*`. Spec was updated; no work needed.

## Cross-slice tickets opened by this slice

- [[_inbox/cross-slice/slice-sdk-py-otel-spans|slice-sdk-py-otel-spans]] — add OpenTelemetry span emission (opt-in via extras / soft import); unskips Test Contract bullet 16.

## PR links

- [#90](https://github.com/ericmey/musubi/pull/90) — `slice/slice-sdk-py` → `v2`.
