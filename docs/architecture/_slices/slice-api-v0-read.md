---
title: "Slice: Canonical API v0.1 — read surface"
slice_id: slice-api-v0-read
section: _slices
type: slice
status: in-progress
owner: vscode-cc-sonnet47
phase: "7 Adapters"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-config]]", "[[_slices/slice-auth]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-api-v0-write]]", "[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-sdk-py]]"]
---

# Slice: Canonical API v0.1 — read surface

> HTTP read surface + scaffolding. Every GET endpoint defined in canonical-api.md, plus the auth middleware + OpenAPI spec + error taxonomy + pagination + health probes that every adapter and the write-side slice inherit.

**Phase:** 7 Adapters · **Status:** `in-progress` · **Owner:** `vscode-cc-sonnet47`

**Split origin:** this slice was split from the original `slice-api-v0` (closed Issue #6) because the combined read+write scope would exceed the 800 LoC PR cap. Write-side is `slice-api-v0-write`, which depends on this slice.

## Specs to implement

- [[07-interfaces/canonical-api]] — read-side endpoints only (GET across all 10 categories)
- [[07-interfaces/contract-tests]] — read-side test cases only (Retrieve, Thoughts list, Artifact get, Lifecycle list, Auth failure tests)

Every Test Contract bullet in the read-side of those specs must land in one of three Closure states at handoff. Write-side bullets are explicitly deferred to `slice-api-v0-write` in the work log.

## Owned paths (you MAY write here)

- `src/musubi/api/` — FastAPI routers, pydantic response models, auth middleware, error mapping, OpenAPI generation. **Read-side endpoints + shared scaffolding only.**
- `tests/api/` — read-side contract tests + unit tests for shared scaffolding.
- `openapi.yaml` — initial version with read-side + scaffolding. Write-side slice extends it.

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/` — plane implementations; you ROUTE to their public surfaces, you do not own them
- `src/musubi/retrieve/` — called from retrieval endpoints, not modified
- `src/musubi/lifecycle/` — not touched from this slice
- `src/musubi/types/` — only `slice-types` writes here
- `src/musubi/adapters/` — future slices
- `proto/` — gRPC schemas; defer to a future grpc-specific slice or the write-side if gRPC for v0 is spec-mandated

## Depends on

All satisfied on v2 HEAD:
- [[_slices/slice-types]] (done)
- [[_slices/slice-config]] (in-review-as-flipped; functionally done)
- [[_slices/slice-auth]] (done)
- [[_slices/slice-plane-episodic]] (in-review-as-flipped; functionally done)
- [[_slices/slice-plane-curated]] (done)
- [[_slices/slice-plane-artifact]] (done)

## Unblocks

- [[_slices/slice-api-v0-write]] — write-side; inherits auth + OpenAPI + error scaffolding from this slice
- Read-side paths of adapters: [[_slices/slice-adapter-mcp]], [[_slices/slice-adapter-livekit]], [[_slices/slice-adapter-openclaw]], [[_slices/slice-sdk-py]]

## What this slice delivers

1. **FastAPI app scaffolding** — `src/musubi/api/app.py` or similar; CORS + middleware chain + OpenAPI auto-generation.
2. **Auth middleware** — consumes the token interface from `slice-auth`; applied as a FastAPI dependency on every authenticated endpoint. Correlation-ID propagation.
3. **Error taxonomy** — typed HTTP error mapping from `Result[T, E]` upstream patterns: every `Err(TransitionError)`, `Err(RetrievalError)`, etc. becomes an appropriate 4xx/5xx with correlation id + structured body per the spec's error section.
4. **Pagination shapes** — cursor-based per canonical-api.md; reusable pydantic response model.
5. **Health + readiness endpoints** — `/health`, `/ready` per spec; ready checks call Qdrant + Ollama + TEI health behind the scenes.
6. **GET endpoints for every plane + retrieval** — episodic, curated, concept, artifact, thoughts list, retrieval (hybrid + future deep/fast), lifecycle reads, contradictions list, ops reads, namespaces list. Each routes to the correct plane's public method; no plane internals touched.
7. **`openapi.yaml`** — canonical spec file, auto-generated from FastAPI routes + pydantic response models; committed to the repo.
8. **Contract tests** — read-side bullets from `contract-tests.md`: auth failures, pagination edge cases, ETag handling if spec mandates, 404/4xx shapes per endpoint, readiness failure modes.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every read-side Test Contract bullet in the linked spec(s) is a passing test.
- [ ] `openapi.yaml` committed and validates against the actual routes (use a FastAPI openapi dump + lint).
- [ ] Branch coverage ≥ 85 % on owned paths (`src/musubi/api/` is not in the 90 % plane/retrieve floor).
- [ ] Slice frontmatter flipped `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.
- [ ] PR body first line is `Closes #<N>.` where `<N>` is the Issue number created for this slice.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-19 — operator (claude-code-opus47) — slice split from slice-api-v0

- Created from the operator-side slice-reconcile that split the original `slice-api-v0` (closed Issue #6) into `-read` + `-write` to respect the 800 LoC PR cap. See the chore PR that landed this file + the sibling `slice-api-v0-write.md` + closed #6 + opened new Issues.
- Discovered by `vscode-cc-sonnet47` during claim-time brief verification; agent correctly paused per "don't split a slice mid-flight" before claiming.

### 2026-04-19 — vscode-cc-sonnet47 — claim

- Claimed slice atomically via `gh issue edit 70 --add-assignee @me`. Issue #70, PR #73 (draft).
- Branch `slice/slice-api-v0-read` off `v2`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
