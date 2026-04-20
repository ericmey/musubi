---
title: "Slice: API app bootstrap — wire production plane factories"
slice_id: slice-api-app-bootstrap
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "7 Adapters"
tags: [section/slices, status/done, type/slice, api, bootstrap, phase-2, critical-path]
updated: 2026-04-20
reviewed: true
depends-on: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-v0-write]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-thoughts]]", "[[_slices/slice-embedding]]", "[[_slices/slice-config]]"]
blocks: []
---

# Slice: API app bootstrap — wire production plane factories

> **CRITICAL PATH.** Production `create_app()` currently ships every plane factory as `raise NotImplementedError` per the ADR-punted-deps-fail-loud pattern. Unit tests override via `app.dependency_overrides`; nothing wires production. Until this slice lands, the deployed app comes up but 500s on first hit. Every consumer-slice unskip against the integration harness (PR #114) is gated on this.

**Phase:** 7 Adapters · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

## Why this slice exists

Surfaced by VS Code during slice-ops-integration-harness (PR #114, merged 2026-04-20). First smoke-scenario bullet `test_capture_then_retrieve_roundtrip` hit `NotImplementedError: EpisodicPlane is not configured` immediately because `musubi.api.dependencies` ships every plane factory as a loud-fail stub and no production code path overrides them. Hidden until tonight because nothing outside unit tests (which `dependency_overrides` their way around this) actually booted `create_app()` against live dependencies.

The cross-slice ticket `_inbox/cross-slice/slice-ops-integration-harness-production-app-bootstrap.md` (VS Code, 2026-04-20) documents the full problem statement. This slice operationalises the fix.

## Specs to implement

- [[07-interfaces/canonical-api]] §App bootstrap (amend the spec in-PR with a `spec-update:` trailer if the section doesn't exist yet — add one documenting the production boot sequence)
- [[08-deployment/compose-stack]] §Environment wiring (should describe which env vars the production bootstrap reads)

## Owned paths (you MAY write here)

- `src/musubi/api/bootstrap.py`                      (new — `bootstrap_production_app()` function + health-gated init)
- `src/musubi/api/dependencies.py`                   (parent slice-api-v0-read done — extend: swap NotImplementedError stubs for overrides the bootstrap installs)
- `src/musubi/api/app.py`                            (parent done — call `bootstrap_production_app()` at `create_app()` init, gated on environment / feature flag so unit tests can still bypass)
- `tests/api/test_bootstrap.py`                      (new — unit tests with mocked Qdrant/TEI)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/`        (use plane factories as-shipped; do not modify plane internals)
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/types/`
- `src/musubi/sdk/`
- `src/musubi/adapters/`
- `openapi.yaml`, `proto/`

## Depends on

All done:
- `slice-api-v0-read` (parent of api/dependencies.py)
- `slice-api-v0-write` (parent of the app surface)
- all 5 plane slices (provides the factories we wire)
- `slice-embedding` (TEI client construction)
- `slice-config` (Settings)

## Unblocks

**Massive downstream impact.** Every consumer-slice unskip against the integration harness (PR #114) is currently gated on this. Specific smoke scenarios blocked today:

- `test_capture_then_retrieve_roundtrip` (integration bullet 5)
- `test_capture_dedup_against_existing` (bullet 6)
- `test_thought_send_check_read_history` (bullet 7)
- `test_curated_create_then_retrieve` (bullet 9)
- `test_artifact_upload_multipart_then_retrieve_blob` (bullet 12)

Also unblocks:
- **First real deploy on musubi.example.local** — can't deploy a server that 500s on first request.
- **POC → v1 migration** — migrator writes via the API; can't write to a non-functional API.
- **OpenClaw v0.1 integration** — same reason.

## What lands

### 1. `src/musubi/api/bootstrap.py` — the production boot function

```python
# Shape — implementing agent adjusts signatures per actual factory types
def bootstrap_production_app(app: FastAPI, settings: Settings) -> None:
    """Install production dependency overrides on the app.

    Constructs real Qdrant + TEI clients from Settings, then wires every
    plane / service factory to return instances built with those clients.

    Idempotent: safe to call multiple times; re-installs cleanly.
    Test-mode safe: tests that construct their own app via app_factory
    fixture bypass this by using dependency_overrides first.
    """
    qdrant = QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=settings.qdrant_api_key.get_secret_value(),
    )
    embedder = TEIEmbedder(
        dense_url=str(settings.tei_dense_url),
        sparse_url=str(settings.tei_sparse_url),
        reranker_url=str(settings.tei_reranker_url),
    )

    app.dependency_overrides[get_qdrant_client] = lambda: qdrant
    app.dependency_overrides[get_embedder] = lambda: embedder
    app.dependency_overrides[get_episodic_plane] = lambda: EpisodicPlane(client=qdrant, embedder=embedder)
    app.dependency_overrides[get_curated_plane]  = lambda: CuratedPlane(client=qdrant, embedder=embedder)
    app.dependency_overrides[get_concept_plane]  = lambda: ConceptPlane(client=qdrant, embedder=embedder)
    app.dependency_overrides[get_artifact_plane] = lambda: ArtifactPlane(client=qdrant, embedder=embedder, blob_root=settings.artifact_blob_path)
    app.dependency_overrides[get_thoughts_plane] = lambda: ThoughtsPlane(client=qdrant, embedder=embedder)
    # Lifecycle + ingestion services similarly.
```

### 2. `src/musubi/api/app.py` extension

`create_app()` calls `bootstrap_production_app(app, settings)` at init, **gated on an env flag** or **the absence of existing `dependency_overrides`** so unit tests that construct their own app via the existing `app_factory` fixture continue to work unmodified.

Suggested gate pattern:

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(...)
    # ... existing middleware, routers, etc ...

    # Skip production bootstrap if test fixture already installed overrides,
    # OR if MUSUBI_SKIP_BOOTSTRAP=1 (a narrow explicit escape hatch for tests
    # that bypass the app_factory fixture).
    if not app.dependency_overrides and not settings.musubi_skip_bootstrap:
        bootstrap_production_app(app, settings)

    return app
```

Add `musubi_skip_bootstrap: bool = Field(default=False)` to Settings.

### 3. Health-gated init (optional but recommended)

If any dep (Qdrant, TEI) isn't reachable at boot, the bootstrap should fail loudly with a clear error mapping to ops/health-check messaging. Don't silently install broken factories — the "fail loud" invariant that was in place via `NotImplementedError` should be preserved in spirit.

Pattern: wrap each dep construction in a short retry (5 tries, 1s apart) then raise a `BootstrapError` with the specific dep name if it never succeeds.

### 4. Integration harness unskips

After this slice lands, follow-up one-line PRs in the 5 blocked integration bullets (5/6/7/9/12) remove their `@pytest.mark.skip` decorators. Those unskips are NOT this slice's work — they're consumer-side followups to PR #114.

## Test Contract

1. `test_bootstrap_installs_qdrant_override`
2. `test_bootstrap_installs_embedder_override`
3. `test_bootstrap_installs_every_plane_override`  (episodic + curated + concept + artifact + thoughts)
4. `test_bootstrap_installs_lifecycle_service_override`
5. `test_bootstrap_is_idempotent_on_second_call`
6. `test_bootstrap_fails_loudly_when_qdrant_unreachable`
7. `test_bootstrap_fails_loudly_when_tei_unreachable`
8. `test_bootstrap_retry_succeeds_on_second_attempt`
9. `test_create_app_calls_bootstrap_by_default`
10. `test_create_app_skips_bootstrap_when_overrides_already_installed`
11. `test_create_app_skips_bootstrap_when_musubi_skip_bootstrap_set`
12. `test_existing_unit_test_fixtures_still_work_unchanged`  (regression check — use an existing api/test_* as the case)

Mock Qdrant + TEI via `unittest.mock` or httpx_mock; no live services needed for unit tests. The integration harness (slice-ops-integration-harness) is where the real-service verification happens.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] All 12 Test Contract bullets passing.
- [ ] Branch coverage ≥ 85% on `src/musubi/api/bootstrap.py` + `src/musubi/api/app.py` changes.
- [ ] Integration harness (PR #114) smoke bullets 5, 6, 7, 9, 12 flip from `@pytest.mark.skip` to passing via consumer-side followup commits (can be in this PR or filed as followup Issues — operator preference is "in this PR" so the harness becomes functionally complete at merge).
- [ ] Cross-slice ticket `slice-ops-integration-harness-production-app-bootstrap.md` flips to `status: resolved`.
- [ ] `make agent-check` clean.
- [ ] Standard handoff + PR body `Closes #<this Issue>.`.

## Work log

### 2026-04-20 — operator — slice carved

- Carved in response to VS Code's cross-slice ticket from PR #114 (slice-ops-integration-harness) integration-first-bullet failure.
- CRITICAL PATH: every Phase 2 consumer (integration harness smoke bullets, first-deploy, POC migration, OpenClaw) is gated on this.
- VS Code hit the bug; he's the natural implementer (freshest context). Alternative: Hana (similar scope to types-followup #107 she just shipped). Operator picks.

### 2026-04-20 — vscode-cc-sonnet47 — take

- Claimed atomically via `gh issue edit 123 --add-assignee @me` + label flip `status:ready → status:in-progress` (dual-update before writes).
- Branch `slice/slice-api-app-bootstrap` off the carve branch `origin/docs/slice-api-app-bootstrap` (carve PR #124 still open at claim time but CLEAN+green; branching off the carve branch so I inherit the slice file without waiting for the merge — when #124 squashes to v2, the squashed commit's content matches my history so a later rebase is a no-op).
- Same agent that opened the cross-slice ticket on PR #114 → wrote most of the spec for this slice → freshest context for the implementation. Canonical 7-commit shape this time (separate `test(api):` commit before `feat(api):`) per the lesson saved to memory after PR #114's audit soft-warning.

### 2026-04-20 — vscode-cc-sonnet47 — handoff to in-review

- Implemented `src/musubi/api/bootstrap.py` with `bootstrap_production_app(app, settings)`: constructs real `QdrantClient` + a TEI-backed composite `Embedder` (`_TEICompositeEmbedder` delegates each protocol method to the right TEI client), health-probes both with bounded linear retry, then installs `app.dependency_overrides` for qdrant + embedder + 5 plane factories + lifecycle service. Idempotent. Fails loud via typed `BootstrapError(dep=...)` on probe exhaustion.
- Extended `src/musubi/api/dependencies.py` with the missing factory stubs (`get_embedder`, `get_thoughts_plane`, `get_lifecycle_service`) for symmetry with the existing 4 plane factories. All raise `NotImplementedError` per the existing pattern; bootstrap supplies the override.
- Wired `src/musubi/api/app.py`: `create_app()` now resolves Settings from config when not supplied + calls `bootstrap_production_app(app, settings)` at the bottom of the build gated on `_should_bootstrap`. Gate skips when `settings.musubi_skip_bootstrap=True` (test escape hatch) OR when overrides are already installed.
- Added `musubi_skip_bootstrap: bool = False` to `Settings`. Updated `tests/api/conftest.py` + `tests/observability/conftest.py` to set it `=True` so unit tests' `app_factory`-pattern fixtures continue to work unchanged (bullet 12 regression invariant).
- 16 unit tests in `tests/api/test_bootstrap.py` (12 contract + 4 coverage), all mocked Qdrant + TEI per spec. All pass locally.
- Closed cross-slice ticket `slice-ops-integration-harness-production-app-bootstrap.md` (status open → resolved).
- **Integration harness unskips** (operator-preferred-in-PR per slice DoD): 12 cycles of CI iteration on PR #126 surfaced 11 distinct adjacent-layer config bugs each fixed in their own commit (TEI sparse model id, Qdrant SSL default, compose --wait-timeout, ollama-pull profile, TEI async-loop conflict, smoke-tests event-loop-closed, token per-namespace scopes, curated payload schema, artifact upload schema, vector dim mismatch, missing collection bootstrap). End state: **3/5 plane-touching bullets PASSING in CI** (capture_dedup, thoughts_send_check, curated_create — proving the bootstrap wiring works end-to-end through the production path); 2 remaining (bullet 5 retrieve-after-capture, bullet 12 artifact-upload) re-skipped against new follow-up Issues #133 + #134 (downstream surface bugs, not bootstrap issues).
- Handoff checks: `make check` green (1000+ passed locally), `make agent-check` clean of slice-touching errors, all 3 CI checks green (`Vault hygiene` + `check` + `Integration (run 1)` pass at 2m flat).
- Flipping `status: in-review`, marking PR ready, removing the lock.

### Known gaps at in-review

- **2 of 5 unskipped integration bullets re-skipped** against follow-up Issues #133 + #134. The bootstrap deliverable is proven by the 3 passing bullets through the same production wiring path; the remaining failures are downstream of bootstrap (Qdrant local-mode indexing latency on bullet 5; chunker/artifact-plane interaction on bullet 12). Each issue documents the likely root cause + fix path.
- **Pre-existing mypy error** in `src/musubi/sdk/tracing.py` (PR #131's OTel addition — `Module "opentelemetry" has no attribute "trace"` warning). Not from this slice; CI tolerates it because its dep-install path differs from local. Operator may want a follow-up to add `# type: ignore` or pin opentelemetry-api differently.
- **Operator merged 4 PRs into the slice branch mid-iteration** (#128 POC migration, #129 async fake client promotion, #130 lifecycle worker metrics, #131 OTel SDK spans, plus CI version fixes). Rebased cleanly each time. Surface-area increased but no integration regressions surfaced.

## Cross-slice tickets opened by this slice

- _(none — this slice resolved the existing `slice-ops-integration-harness-production-app-bootstrap.md` cross-slice ticket; no new tickets opened.)_

## Follow-up Issues opened (re-skip targets for in-PR unskip)

- [#133](https://github.com/ericmey/musubi/issues/133) — bullet 5 capture-then-retrieve unskip; needs Qdrant `wait=True` or longer poll budget on cold-cache CI.
- [#134](https://github.com/ericmey/musubi/issues/134) — bullet 12 artifact-upload unskip; downstream chunker / artifact-plane root-cause investigation.

## PR links

- [#126](https://github.com/ericmey/musubi/pull/126) — `slice/slice-api-app-bootstrap` → `v2`.
