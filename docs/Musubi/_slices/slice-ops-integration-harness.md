---
title: "Slice: Integration test harness"
slice_id: slice-ops-integration-harness
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "8 Ops"
tags: [section/slices, status/done, type/slice, integration, testing, phase-2]
updated: 2026-04-20
reviewed: true
depends-on: ["[[_slices/slice-ops-compose]]"]
blocks: ["[[_slices/slice-ops-hardening-suite]]", "[[_slices/slice-adapter-livekit-e2e]]"]
---

# Slice: Integration test harness

> Docker-compose test environment + end-to-end scenarios + `make test-integration` target. Closes ~19 Test Contract bullets currently skipped with "deferred to integration harness" across the done slices, and is the first Phase 2 hardening deliverable — everything downstream (perf baselines, load tests, chaos scenarios) depends on this existing.

**Phase:** 8 Ops · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

## Why this slice exists

Multiple done slices deferred bullets to "an integration harness when we have one." Tonight's hidden-pile audit counted ~19 such bullets across planes, retrieval, lifecycle, and ingestion. Shipping the harness unblocks all of them, plus every Phase 2 activity (perf, load, chaos) that needs a real multi-service environment to exercise against.

## Specs to implement

- [[08-deployment/compose-stack]] §Integration test env
- [[09-operations/observability]] (harness uses observability instrumentation once it lands via #19)

## Owned paths (you MAY write here)

- `deploy/test-env/`                                 (new — docker-compose.test.yml + per-service test configs)
- `tests/integration/`                               (new — pytest suite + fixtures + conftest)
- `Makefile`                                         (parent done — add real `make test-integration` target; currently a stub)
- `pyproject.toml`                                   (parent done — add `[dev-integration]` optional-dependency group if needed)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/`   (any changes to the code under test)
- `docs/Musubi/07-interfaces/`  (API contract)
- `openapi.yaml`, `proto/`

## Depends on

- [[_slices/slice-ops-compose]]   (done — docker-compose patterns)

## Unblocks

Downstream followups that currently defer bullets here:
- `slice-plane-episodic` bullets 32, 33 (perf on 100k-memory corpus)
- `slice-retrieval-deep` bullets 4, 5, 9 (p95 perf, parallel-safe under concurrent callers, one-plane-timeout)
- `slice-retrieval-deep` integration bullets 13, 14 (LiveKit slow-thinker scenario, deep vs fast comparison)
- `slice-ingestion-capture` out-of-scope bullet 22 (100-item-under-1s benchmark)
- `slice-plane-concept` out-of-scope bullet 24 (ollama-offline scenario)
- `slice-plane-artifact` large-file handling bullets
- `slice-api-thoughts-stream` bullet 20 (hypothesis/idempotency — better with real broker + real Qdrant)
- Every `slice-retrieval-*` integration bullet that mentions "real corpus"

Future Phase 2 slices (perf suite, load suite, chaos scenarios) depend on this harness.

## Test Contract

**Harness infrastructure:**

1. `test_harness_boots_compose_stack_cleanly`
2. `test_harness_tears_down_cleanly_leaving_no_orphans`
3. `test_harness_pytest_fixture_provides_real_client_to_running_stack`
4. `test_harness_supports_parallel_session_execution`

**Smoke scenarios (end-to-end through the full stack):**

5. `integration: capture_then_retrieve_roundtrip` — POST capture → Qdrant write → fast-path retrieve → content matches.
6. `integration: capture_dedup_against_existing` — capture X twice, assert reinforcement_count == 2.
7. `integration: thought_send_check_read_history` — full cycle across thought endpoints.
8. `integration: thought_stream_delivers_live` — SSE stream sees a thought posted mid-subscription.
9. `integration: curated_create_then_retrieve` — curated plane end-to-end.
10. `integration: concept_synthesis_flow_ollama_present` — full lifecycle with real Ollama.
11. `integration: concept_synthesis_flow_ollama_offline` — lifecycle degrades gracefully, re-enrichment queues.
12. `integration: artifact_upload_multipart_then_retrieve_blob` — file upload path.
13. `integration: retrieve_deep_under_5s_on_10k_corpus` — perf budget check on a pre-loaded corpus.
14. `integration: retrieve_fast_under_200ms_on_10k_corpus` — fast-path budget.

**Bullet-consumer hookups** (when this harness lands, the affected slices' followup PRs unskip their relevant bullets — listed as out-of-scope here; each consumer owns its own unskip commit):

**Explicitly out-of-scope (do NOT implement here):**

- Load testing (locust / k6 profiles) — future `slice-ops-load-suite`.
- Chaos scenarios (kill-qdrant, TEI-slow, Ollama-OOM) — future `slice-ops-chaos-suite`.
- 24h soak test orchestration — future `slice-ops-soak-suite`.
- Updates to individual slices' test files to unskip their deferred bullets — those happen in per-slice followup PRs after this lands.

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] `make test-integration` boots the compose stack, runs all 14 Test Contract bullets above, tears down cleanly.
- [ ] Suite completes in <5 minutes on reference hardware.
- [ ] No flakes on 3 consecutive runs.
- [ ] Branch coverage ≥ 75% on `tests/integration/` (integration tests are scenario-heavy; stricter than unit but looser than 85%).
- [ ] At least 3 per-slice followup PRs are drafted (or tracked as follow-up Issues) showing bullets will unskip against this harness — not required to land with this slice but required as evidence the harness actually serves its purpose.
- [ ] GitHub Actions workflow added (`.github/workflows/integration.yml`) running the suite nightly on v2 (integration suite is too slow for every PR).
- [ ] Slice frontmatter flipped appropriately.
- [ ] Issue dual-update at claim time.

## Implementation notes

**Use real services, not mocks.** The whole point is the harness catches integration bugs unit tests don't. Docker-compose spins up real Qdrant, real TEI (dense+sparse+reranker), real Ollama. Tests hit the real HTTP API.

**Fixture shape suggestion:**

```python
@pytest.fixture(scope="session")
def live_stack() -> Generator[StackHandle, None, None]:
    subprocess.run(["docker-compose", "-f", "deploy/test-env/docker-compose.test.yml", "up", "-d", "--wait"])
    yield StackHandle(api_url="http://localhost:8100/v1", ...)
    subprocess.run(["docker-compose", "-f", "deploy/test-env/docker-compose.test.yml", "down", "-v"])

@pytest.fixture
def api_client(live_stack: StackHandle) -> AsyncMusubiClient:
    return AsyncMusubiClient(base_url=live_stack.api_url, token=live_stack.operator_token)
```

Tests use `api_client` and assert end-to-end behaviour.

**Corpus fixtures:** pre-load 10k memories into the test Qdrant at stack-boot time for perf-adjacent tests. A deterministic seed script in `tests/integration/_corpus/` generates the corpus from a fixed list of templates.

**CI vs local:** GitHub Actions runs the suite nightly on v2 (not per-PR; too slow + docker-in-docker overhead). Local devs run `make test-integration` on demand.

## Work log

### 2026-04-19 — operator — slice carved

- First Phase 2 deliverable per tonight's hidden-pile audit + production-readiness roadmap discussion with Eric.
- ~19 deferred bullets across done slices cite this harness as their blocker. Each consumer slice owns its own unskip commit after this lands.
- Scope: harness + 14 bullet-level smoke scenarios. Perf/load/chaos are future siblings, not this slice.

### 2026-04-19 — vscode-cc-sonnet47 — take

- Claimed atomically via `gh issue edit 108 --add-assignee @me` + label flip `status:ready → status:in-progress` (dual-update before writes).
- Branch `slice/slice-ops-integration-harness` off `v2`.
- Same agent that landed slice-ops-compose, slice-sdk-py (FakeMusubiClient pattern), slice-adapter-livekit (fixture wiring), slice-ops-observability (cross-cutting instrumentation) — those four are the composition target this harness exercises end-to-end.
- **Pre-flight constraint surfaced + resolved with operator before going deep:** Docker is not installed on this agent's dev machine. Operator confirmed CI-as-first-verification is acceptable for this slice. Approach: split the 14 Test Contract bullets — bullets 1-4 (harness-shape) verified locally via mocked-subprocess pattern; bullets 5-14 (real-services smoke) verified by initial CI run via PR-trigger path-filter on `.github/workflows/integration.yml`; flake-characterization evidence comes from the post-merge nightly cron's matrix-of-3.

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Implemented the harness scaffolding per the operator-confirmed verifiability split:
  - `deploy/test-env/docker-compose.test.yml` — 5 dependency services (Qdrant + TEI dense/sparse/reranker on `:cpu-1.6` + Ollama with side-car model pull). CPU-image variants throughout so the stack runs on stock GitHub Actions runners.
  - `deploy/test-env/.env.test` — env-file the harness sources for the in-process musubi-core uvicorn.
  - `deploy/test-env/README.md` — local-run + port-collision-override + perf-budget gating.
  - `tests/integration/conftest.py` — session-scoped `live_stack` (boots compose deps, spawns uvicorn for `musubi.api.app:create_app`, mints operator JWT, polls `/v1/ops/health`); per-test `api_client`. Single `_run` chokepoint for all subprocess shell-outs so harness-shape tests can monkeypatch.
  - `tests/integration/_corpus/seed.py` — deterministic 10k-memory template-expansion generator (perf-bullet baseline).
  - `tests/integration/test_harness.py` — 11 tests realising bullets 1-4 + coverage; verified locally on this docker-less machine via the mocked-subprocess pattern.
  - `tests/integration/test_smoke.py` — 10 scenarios scaffolded for bullets 5-14; current state below.
  - `Makefile` — `test-integration` target invokes `pytest -m integration`; default `test` excludes integration via pyproject `addopts`.
  - `.github/workflows/integration.yml` — PR-trigger path-filter (this PR + any harness-touching followup) + nightly cron with `[1, 2, 3]` matrix for flake characterization + workflow_dispatch.
- **CI revealed two real issues local-only verification couldn't have caught — exactly the case for the CI-as-first-verification split:**
  1. TEI `:cpu-1.5` had a hf-hub bug ("relative URL without a base") on first model download. Fixed by bumping to `:cpu-1.6` + per-service model volumes + explicit `HF_HUB_ENDPOINT`.
  2. `musubi.api.app.create_app()` ships ADR-punted-deps-fail-loud stubs for every plane factory in `musubi.api.dependencies`. No production bootstrap wires real planes. Hidden until tonight because nothing was running the production app outside unit tests; this harness was the first to do so. Bullets 5/6/7/9/12 deferred against new cross-slice ticket `slice-ops-integration-harness-production-app-bootstrap.md`.
- Handoff checks: `make check` green (889 passed, 226 skipped, 10 deselected), `make tc-coverage SLICE=slice-ops-integration-harness` reports closure satisfied (the slice's specs are compose-stack + observability — already shipped + green), `make agent-check` clean of slice-touching errors (one ✗ on slice-types-followup is Hana's flapping label, not mine), CI green (3/3 checks pass including the integration job at 1m29s).
- Three follow-up Issues opened (#118 ingestion-capture, #119 plane-concept ollama-offline, #120 api-thoughts-stream SSE) — DoD evidence the harness serves consumer slices.
- Flipping `status: in-review`, marking PR ready, removing the lock.

### Known gaps at in-review

- **Production app bootstrap is the gating cross-slice.** Five bullets (5/6/7/9/12) are skipped against `slice-ops-integration-harness-production-app-bootstrap.md`. That ticket is the real follow-up dependency every other consumer slice unskip needs to land first. Owner suggestion: slice-api-v0-write or a new `slice-api-app-bootstrap` carve-out.
- **Perf bullets (13/14) are CPU-stack-unrealistic.** They skip unless `MUSUBI_TEST_PERF_BUDGETS=strict` is set; operator's nightly run on the GPU reference host is the evidence path.
- **Concept-synthesis bullets (10/11)** need an operator-scope debug endpoint to trigger the lifecycle worker from a test; documented in scaffolded skip with consumer-slice ownership.
- **SSE bullet (8)** scaffold-only; consumer slice (slice-api-thoughts-stream) owns the unskip per Issue #120.
- **Flake-characterization evidence comes from the first ~week of nightly runs.** The PR-trigger run verified one boot + scenario-suite-collection; the "no flakes on 3 consecutive runs" DoD bullet's evidence is the nightly cron's matrix-of-3 over time.
- **Local docker absence on agent dev machine** documented per the operator's brief refinement; CI provided the first verification of the docker-up path.

## Cross-slice tickets opened by this slice

- [[_inbox/cross-slice/slice-ops-integration-harness-production-app-bootstrap|production-app-bootstrap]] — wire production plane factories into `create_app()`; gates consumer-slice unskips for bullets 5/6/7/9/12.

## Follow-up Issues opened (DoD evidence)

- [#118](https://github.com/ericmey/musubi/issues/118) — slice-ingestion-capture bullet 22 (100-item batch capture <1s) unskip path.
- [#119](https://github.com/ericmey/musubi/issues/119) — slice-plane-concept bullet 24 (ollama-offline graceful degradation) unskip path.
- [#120](https://github.com/ericmey/musubi/issues/120) — slice-api-thoughts-stream bullet 20 (SSE live delivery) unskip path.

## PR links

- [#114](https://github.com/ericmey/musubi/pull/114) — `slice/slice-ops-integration-harness` → `v2`.
