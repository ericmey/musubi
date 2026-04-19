---
title: "Slice: Canonical API v0.1 — write surface"
slice_id: slice-api-v0-write
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "7 Adapters"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-types]]", "[[_slices/slice-auth]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-sdk-py]]"]
---

# Slice: Canonical API v0.1 — write surface

> HTTP write surface. POST / PATCH / DELETE across every plane + lifecycle transition endpoints + write-side contract tests. Inherits auth + OpenAPI + error scaffolding from `slice-api-v0-read`.

**Phase:** 7 Adapters · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

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

### 2026-04-19 — vscode-cc-sonnet47 — claim

- Claimed slice atomically via `gh issue edit 71 --add-assignee @me`. Issue #71, PR #78 (draft).
- Branch `slice/slice-api-v0-write` off `v2`.
- Same agent that landed `slice-api-v0-read` (PR #73) — extending own scaffolding.

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Landed the write surface as 7 router files + 2 middleware modules under `src/musubi/api/`: idempotency cache, rate-limit middleware (path-driven bucket selection because middleware runs before FastAPI routing), POST capture / batch / PATCH / DELETE for episodic + curated, POST reinforce / promote / reject / DELETE for concepts, multipart POST + archive + purge for artifacts, POST send + read for thoughts, POST /v1/lifecycle/transition wrapping the canonical primitive, and POST /v1/retrieve/stream emitting NDJSON.
- `openapi.yaml` extended from 21 → 31 routable paths; the runtime-vs-snapshot drift test from `slice-api-v0-read` still guards both directions.
- `pyproject.toml` adds `python-multipart>=0.0.9` (required by FastAPI's `File`/`Form` parsing for the artifact upload route).
- Tests: 36 passing + 1 skipped-with-reason for the 6 spec write-side bullets, 9 contract-tests example cases (capture happy / dedup / batch / PATCH / DELETE / promote / archive / send / lifecycle transition / etc.), plus 13 coverage tests. Branch coverage on `src/musubi/api/` is **~89 %** (gate 85 %).
- Handoff checks: `make check` 671 passed / 169 skipped clean, `make tc-coverage SLICE=slice-api-v0-write` exits 0 (16 passing + 11 skipped), `make agent-check` clean (no `^  ✗` errors; only `⚠` warnings + drift on three parallel agents' slices), `gh pr view 78 --json mergeStateStatus` is `CLEAN` + `mergeable=MERGEABLE`, `gh pr checks 78` reports both checks pass remotely, PR body first line is `Closes #71.`, `git ls-files src/musubi/api/ tests/api/ openapi.yaml` shows 33 files all present + the `feat(api)` commit at `4061e52` touches them.

#### Architectural notes for the reviewer

- **Rate-limit bucket selection is path-driven, not operation_id-driven.** First implementation tried to read each route's `operation_id` to pick the bucket, but Starlette/FastAPI middleware runs BEFORE routing — `request.scope["route"]` is `None` at middleware time. Switched to a path-prefix → bucket map in `_PATH_TO_BUCKET`. First match wins. New write routes adding their own bucket need a one-line addition to that tuple.
- **Operator detection in middleware is best-effort + unverified.** The rate-limit middleware needs to know whether the bearer carries `operator` scope to pick the 10x ceiling — but full token verification (`authenticate_request`) hasn't run yet. So `_is_operator()` decodes the JWT body without verifying. The actual verification happens later in the per-route auth dep, which gates 401 / 403. This is a deliberate split: rate-limit ceiling is a soft signal; auth gate is the security boundary.
- **Body-namespace POST endpoints validate scope manually**, mirroring the read-side pattern from `slice-api-v0-read` (POST `/v1/retrieve`, POST `/v1/thoughts/check`). The query-param-based `require_auth()` dep doesn't help when the namespace lives in the body — handlers call `_check_body_scope(request, body.namespace, settings)` after pydantic-parsing the body.
- **PATCH endpoints reject state-managed fields with typed BAD_REQUEST.** The request models are `extra="allow"` so the handler can name the offending field (`state`, `version`, etc.) instead of falling back to FastAPI's default 422. State changes route through POST `/v1/lifecycle/transition` per the canonical primitive.
- **DELETE endpoints route through `lifecycle.transitions.transition()`** for soft archive — the lifecycle ledger records every state change. `?hard=true` requires operator scope; on episodic that path is implemented (Qdrant `delete` by filter), on artifact it's a placeholder `202 purge-scheduled` because the blob-store wiring isn't shipped yet.
- **Idempotency cache is in-memory + process-local.** Fine for a single-worker dev deploy. Multi-worker production needs Redis or Kong; the swap-out is a Protocol-keyed dependency (`get_idempotency_cache`) so callers don't change. The `_GLOBAL_CACHE` is reset between tests via an autouse conftest fixture.
- **NDJSON streaming uses Starlette's `StreamingResponse`** with `application/x-ndjson` media-type. First-cut wraps `EpisodicPlane.query` in an async generator emitting one line per result. Cross-plane streaming + reranking is a downstream `slice-retrieval-orchestration` follow-up.

#### Test Contract coverage matrix

| # | Bullet | State | Where |
|---|---|---|---|
| 9 | `test_multipart_upload_for_artifacts` | ✓ passing | `tests/api/test_api_v0_write.py` |
| 10 | `test_idempotency_key_roundtrip` | ✓ passing | `tests/api/test_api_v0_write.py` |
| 11 | `test_idempotency_key_expires_after_24h` | ✓ passing | `tests/api/test_api_v0_write.py` |
| 13 | `test_rate_limit_enforces_token_bucket` | ✓ passing | `tests/api/test_api_v0_write.py` |
| 14 | `test_rate_limit_operator_scope_10x_limit` | ✓ passing | `tests/api/test_api_v0_write.py` |
| 15 | `test_ndjson_retrieve_stream_yields_per_result` | ✓ passing | `tests/api/test_api_v0_write.py` |
| — | `test_protobuf_via_grpc_matches_rest_semantics_writes` | ⏭ skipped | deferred → future `slice-api-grpc` (proto/ forbidden in this slice) |

## Cross-slice tickets opened by this slice

- _(none — see "Architectural notes for the reviewer" in the handoff entry above for two future-work items the renderer carries placeholders for: artifact `/purge` blob-store wiring (deferred to a future blob-store slice); cross-plane retrieval streaming via `slice-retrieval-orchestration`. Both ship as degenerate-OK placeholders today.)_

## PR links

- #78 — `feat(api): slice-api-v0-write` (in-review)
