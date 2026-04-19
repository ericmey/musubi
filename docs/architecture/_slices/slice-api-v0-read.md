---
title: "Slice: Canonical API v0.1 — read surface"
slice_id: slice-api-v0-read
section: _slices
type: slice
status: in-review
owner: vscode-cc-sonnet47
phase: "7 Adapters"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-config]]", "[[_slices/slice-auth]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-artifact]]"]
blocks: ["[[_slices/slice-api-v0-write]]", "[[_slices/slice-adapter-livekit]]", "[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-openclaw]]", "[[_slices/slice-sdk-py]]"]
---

# Slice: Canonical API v0.1 — read surface

> HTTP read surface + scaffolding. Every GET endpoint defined in canonical-api.md, plus the auth middleware + OpenAPI spec + error taxonomy + pagination + health probes that every adapter and the write-side slice inherit.

**Phase:** 7 Adapters · **Status:** `in-review` · **Owner:** `vscode-cc-sonnet47`

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

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Landed `src/musubi/api/`: `create_app` factory + correlation-ID middleware + typed-error envelope, FastAPI bearer-auth dependency wrapping `musubi.auth.authenticate_request`, cursor-paginated `Page[T]` envelope wrapping Qdrant's native scroll offset, and 10 router groups (ops, episodic, curated, concepts, artifacts, thoughts, retrieve, lifecycle, contradictions, namespaces) totaling 21 routable paths.
- `openapi.yaml` snapshot committed at the repo root, generated from the runtime FastAPI spec per ADR-0013. Two tests (`test_committed_openapi_yaml_includes_read_paths` + `test_runtime_openapi_matches_committed_paths`) guard against drift between code and snapshot.
- `pyproject.toml`: added `fastapi>=0.115` (authorized by ADR-0011 + ADR-0013, which already established FastAPI as the chosen framework) and `types-PyYAML` for the snapshot test.
- Tests: 21 passing + 9 skipped-with-reason for the spec's 18 Test Contract bullets, all 9 contract-tests-repo meta bullets marked deferred-with-reason, plus 18 coverage tests (router happy paths, auth edge cases, correlation-ID middleware, malformed cursor handling, ADR-punted DI factories). Coverage on `src/musubi/api/` is **91 % branch** (gate 85 %).
- Five+ handoff checks: `make check` 568 passed / 125 skipped clean, `make tc-coverage SLICE=slice-api-v0-read` exits 0 (Closure Rule satisfied), `make agent-check` clean (no `^  ✗` errors; only pre-existing `⚠` warnings + drift on three other agents' slices), `gh pr view 73 --json mergeStateStatus` is `CLEAN` (not DIRTY), `gh pr checks 73` reports both checks pass remotely, PR body first line is `Closes #70.`.

#### Architectural notes for the reviewer

- **FastAPI dep added** — `pyproject.toml` gains `fastapi>=0.115`. This is not a new top-level dependency without an ADR: ADR-0011 chose FastAPI as the canonical framework, and ADR-0013 codified pydantic-first OpenAPI generation. The slice ships the framework's first concrete use.
- **Method ownership respected** — every router handler delegates to a plane's public method (`get`, `query`) for single-row + dense-search reads. Bulk-listing endpoints (`list_memories`, `list_artifacts`, etc.) scroll Qdrant directly via `routers/_scroll.py` because the planes don't expose a `list_by_namespace` surface (and shouldn't — the planes are write-side authorities). The scroll helper is read-only — no plane mutation outside `transition()`. If the reviewer wants this consolidated into a plane method, that's a follow-up cross-slice across each plane.
- **Three Protocols-via-DI for plane factories** — `get_episodic_plane` / `get_curated_plane` / `get_concept_plane` / `get_artifact_plane` raise `NotImplementedError` by default, mirroring the `_NotConfiguredOllama` pattern this slice's predecessor codified. Tests override via `app.dependency_overrides`; production wiring lives in slice-ops-compose's bootstrap.
- **Auth flexibility** — query-param-namespace endpoints use `require_auth()` as a FastAPI dep; body-namespace endpoints (POST `/v1/thoughts/check`, POST `/v1/retrieve`) parse the body first then validate scope manually. Both paths share `musubi.auth.authenticate_request` so the scope-check semantics are identical.
- **Error taxonomy** — `APIError` raised from routers maps to the spec's `{error: {code, detail, hint}}` envelope. FastAPI's default 422 (request validation) is re-shaped to `BAD_REQUEST` so adapters see one error format across every endpoint.
- **NDJSON streaming + multipart upload + idempotency-key + rate-limit middleware** are all explicitly deferred to `slice-api-v0-write` per the read/write split. Each Test Contract bullet for those features is `@pytest.mark.skip` with a named follow-up.
- **gRPC + `proto/`** is deferred to a future `slice-api-grpc` per the read-slice scope; `proto/` is in this slice's `forbidden_paths`.

#### Test Contract coverage matrix

| # | Bullet | State | Where |
|---|---|---|---|
| 1 | `test_openapi_generated_matches_pydantic` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 2 | `test_all_documented_endpoints_routable` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 3 | `test_error_shape_consistent_across_endpoints` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 4 | `test_missing_token_returns_401` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 5 | `test_out_of_scope_returns_403` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 6 | `test_operator_scope_accesses_admin_endpoints` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 7 | `test_json_default` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 8 | `test_protobuf_via_grpc_matches_rest_semantics` | ⏭ skipped | deferred → future `slice-api-grpc` |
| 9 | `test_multipart_upload_for_artifacts` | ⏭ skipped | deferred → `slice-api-v0-write` |
| 10 | `test_idempotency_key_roundtrip` | ⏭ skipped | deferred → `slice-api-v0-write` |
| 11 | `test_idempotency_key_expires_after_24h` | ⏭ skipped | deferred → `slice-api-v0-write` |
| 12 | `test_v1_path_lives_alongside_v2_when_present` | ⊘ out-of-scope | declared in work log: no `/v2/` to live alongside today |
| 13 | `test_rate_limit_enforces_token_bucket` | ⏭ skipped | deferred → `slice-api-v0-write` |
| 14 | `test_rate_limit_operator_scope_10x_limit` | ⏭ skipped | deferred → `slice-api-v0-write` |
| 15 | `test_ndjson_retrieve_stream_yields_per_result` | ⏭ skipped | deferred → `slice-api-v0-write` |
| 16 | `test_cursor_roundtrip_exhausts_list` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 17 | `test_cursor_opaque_to_client` | ✓ passing | `tests/api/test_api_v0_read.py` |
| 18 | `test_contract_suite_runs_end_to_end` | ⊘ out-of-scope | declared in work log: contract suite lives in future `musubi-contract-tests` repo |

Plus the 9 `contract-tests.md` "Test contract (meta)" bullets — all stubs marked `@pytest.mark.skip(reason="deferred to musubi-contract-tests repo: ...")` since they are tests OF the contract-tests repo (a separate Python package per ADR-0011), not the musubi-core API codebase.

## Cross-slice tickets opened by this slice

- _(none — bulk-listing pagination consolidation into plane methods is noted in the handoff Architectural notes as a candidate cross-slice follow-up across each plane, but not opened today.)_

## PR links

- #73 — `feat(api): slice-api-v0-read` (in-review)
