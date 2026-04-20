---
title: "Slice: API app bootstrap — wire production plane factories"
slice_id: slice-api-app-bootstrap
section: _slices
type: slice
status: ready
owner: unassigned
phase: "7 Adapters"
tags: [section/slices, status/ready, type/slice, api, bootstrap, phase-2, critical-path]
updated: 2026-04-20
reviewed: false
depends-on: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-v0-write]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-thoughts]]", "[[_slices/slice-embedding]]", "[[_slices/slice-config]]"]
blocks: []
---

# Slice: API app bootstrap — wire production plane factories

> **CRITICAL PATH.** Production `create_app()` currently ships every plane factory as `raise NotImplementedError` per the ADR-punted-deps-fail-loud pattern. Unit tests override via `app.dependency_overrides`; nothing wires production. Until this slice lands, the deployed app comes up but 500s on first hit. Every consumer-slice unskip against the integration harness (PR #114) is gated on this.

**Phase:** 7 Adapters · **Status:** `ready` · **Owner:** `unassigned`

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

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
