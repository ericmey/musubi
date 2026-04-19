---
title: "Slice: Canonical API v0.1 — write surface"
slice_id: slice-api-v0-write
section: _slices
type: slice
status: ready
owner: unassigned
phase: "7 Adapters"
tags: [section/slices, status/ready, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-types]]", "[[_slices/slice-auth]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-sdk-py]]"]
---

# Slice: Canonical API v0.1 — write surface

> HTTP write surface. POST / PATCH / DELETE across every plane + lifecycle transition endpoints + write-side contract tests. Inherits auth + OpenAPI + error scaffolding from `slice-api-v0-read`.

**Phase:** 7 Adapters · **Status:** `ready` · **Owner:** `unassigned`

**Split origin:** split from original `slice-api-v0` (closed Issue #6) to respect the 800 LoC PR cap. See `slice-api-v0-read` for the read-side + scaffolding; this slice extends that foundation with mutations.

## Specs to implement

- [[07-interfaces/canonical-api]] — write-side endpoints only (POST / PATCH / DELETE across all categories; lifecycle transition endpoints)
- [[07-interfaces/contract-tests]] — write-side test cases: capture dedup, artifact upload, thought send + read-state tracking, lifecycle transition errors, rate-limit failure mode

## Owned paths (you MAY write here)

- `src/musubi/api/` — new routers for write endpoints; extend existing error mapping if new Err codes arise
- `tests/api/` — write-side contract tests
- `openapi.yaml` — extend the file landed by `-read` with write-side endpoint definitions

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/` — read-only; you ROUTE to plane mutation methods (create, transition, etc.); you do not modify planes
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/` — you CALL the scheduler / `transition()` primitive; don't reimplement
- `src/musubi/types/`
- `src/musubi/adapters/`

## Depends on

- [[_slices/slice-api-v0-read]] (must be `status: done` before this slice can start) — provides auth middleware, error taxonomy, OpenAPI scaffold, pagination shapes.
- [[_slices/slice-types]] (done)
- [[_slices/slice-auth]] (done)
- [[_slices/slice-plane-episodic]] (functionally done)
- [[_slices/slice-plane-curated]] (done)
- [[_slices/slice-plane-artifact]] (done)

## Unblocks

- [[_slices/slice-ingestion-capture]] — the `POST /capture` endpoint lives in this slice; ingestion-capture wraps it
- Write-side paths of adapters: [[_slices/slice-adapter-mcp]], [[_slices/slice-adapter-livekit]], [[_slices/slice-adapter-openclaw]], [[_slices/slice-sdk-py]]

## What this slice delivers

1. **POST /capture** — dedupes via similarity threshold per spec; writes to episodic plane. 202 Accepted with object_id.
2. **POST /thoughts** — thought-send endpoint; writes via `ThoughtsPlane.send`.
3. **PATCH /episodic/{id}**, **PATCH /curated/{id}**, etc. — per-plane mutation endpoints per spec; routes through each plane's public mutation surface.
4. **POST /lifecycle/transitions** — lifecycle transition endpoint (operator-triggered); wraps `musubi.lifecycle.transitions.transition()`.
5. **POST /artifacts** — artifact upload (content-addressed blob + metadata row); routes to `ArtifactPlane.create()`.
6. **DELETE endpoints** — per spec (hard vs soft; operator-scope gated).
7. **Rate-limit middleware** — per spec; applied to write endpoints.
8. **OpenAPI extension** — adds every write route definition to `openapi.yaml`.
9. **Write-side contract tests** — auth rejection paths, dedup collision shape, rate-limit 429 shape, idempotency headers if spec mandates.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every write-side Test Contract bullet in the linked spec(s) is a passing test.
- [ ] `openapi.yaml` updated — every write route present + schema validated.
- [ ] Branch coverage ≥ 85 % on owned paths.
- [ ] Slice frontmatter flipped `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.
- [ ] PR body first line is `Closes #<N>.`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 — operator (claude-code-opus47) — slice created via slice-api-v0 reconcile

- Created alongside `slice-api-v0-read`; this slice owns the mutation surface.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
