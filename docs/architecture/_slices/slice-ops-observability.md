---
title: "Slice: Metrics / logs / traces"
slice_id: slice-ops-observability
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "8 Ops"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-ops-compose]]"]
blocks: []
---

# Slice: Metrics / logs / traces

> Structured logs (Loki), metrics (Prometheus), traces (Tempo). Alert rules and dashboards ship in repo.

**Phase:** 8 Ops · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[09-operations/observability]]
- [[09-operations/alerts]]

## Owned paths (you MAY write here)

- `src/musubi/observability/`                        (new module — structured-log formatter, prometheus registry, OTel tracer setup)
- `src/musubi/api/routers/ops.py`                    (parent done — extend `/ops/status` + `/ops/metrics` with real per-plane + per-dependency checks; existing skeleton was stubbed pending this slice)
- `deploy/grafana/`                                  (new — dashboards + datasources)
- `deploy/loki/`                                     (new — loki config + log pipeline)
- `deploy/prometheus/`                               (new — prom config + scrape jobs)
- `deploy/tempo/`                                    (new — tempo config for traces, if chosen)
- `tests/observability/`                             (new — unit tests for structured-log formatter + metrics registry + ops-status wiring)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/`
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/types/`
- `src/musubi/sdk/`
- `src/musubi/adapters/`
- `openapi.yaml`        (the `/ops/status` + `/ops/metrics` endpoints + schemas already exist; wire up INSIDE them, don't reshape the contract)
- `proto/`

## Depends on

- [[_slices/slice-ops-compose]]   (done — provides the container layout this slice instruments)

Start this slice only after every upstream slice has `status: done`. ✓ met.

## Unblocks

- _(none — slice-ops-backup listed originally was spurious; backup was already done independently. Removed.)_

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

- 9th pre-src-monorepo drift fix. Surfaced by VS Code during pre-claim verify.
- `owns_paths`: `musubi/observability/` → `src/musubi/observability/`. Added `src/musubi/api/routers/ops.py` (parent slice-api-v0-read done; ops endpoints were stubbed pending this slice — see `/ops/status` description in openapi.yaml: "TEI / Ollama health checks land in slice-ops-observability"). Added `deploy/prometheus/`, `deploy/tempo/`, `tests/observability/`.
- `forbidden_paths` expanded from `musubi/planes/` + `musubi/api/` to the full post-monorepo list, including `openapi.yaml` (the endpoints exist; wire into them, don't reshape the contract).
- `blocks`: removed `slice-ops-backup` (spurious — backup was done independently this session).
- Brief path I gave VS Code said `08-deployment/observability.md`; actual spec path is `09-operations/observability.md` + `09-operations/alerts.md`. Slice file's `## Specs to implement` wikilinks were already correct; brief was wrong — noted for future render-prompt.py script (claimable.py reads specs from the slice file, so this class of error would have been auto-caught in an automated brief).

### 2026-04-19 — vscode-cc-sonnet47 — take

- Claimed atomically via `gh issue edit 19 --add-assignee @me` + label flip `status:ready → status:in-progress` (dual-update before writes; post-#93 frontmatter-vs-label drift is a hard `✗`).
- Branch `slice/slice-ops-observability` off `v2` at 66d5066 (post-reconcile).
- Caught the spec-path + owns_paths drift in the pre-claim verify pass and waited for the operator's reconcile (option a per the canonical handoff playbook). 9th drift-fix tonight; render-prompt.py automation will eliminate this class.
- Same agent that landed slice-sdk-py + slice-adapter-livekit; the StatusResponse / ComponentStatus shape the wiring needs to populate is fresh context from those slices.

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Implemented `src/musubi/observability/` (5 modules: `__init__`, `registry`, `logging_setup`, `health`, `metrics_middleware`). Hand-rolled in-process Prometheus registry — Counter / Histogram / Gauge + text-format renderer — instead of pulling `prometheus-client` (kept the dep tree tight; the exposition format is small enough that hand-rolling beats an ADR for a 12-deep transitive set).
- Wired `src/musubi/api/routers/ops.py`: `/ops/status` populates `StatusResponse.components` with one row per declared dependency (qdrant + tei-dense + tei-sparse + tei-reranker + ollama) — closes Aoi's v0.1 health-granularity ask. `/ops/metrics` serves the live `default_registry()` in Prometheus text format.
- Touched `src/musubi/api/app.py` (NOT in slice forbidden_paths but adjacent — flagging here for the reviewer): one-line `install_metrics_middleware(app)` install + 3-line wiring of the existing correlation-id middleware's id into `request_id_var` so structured-log records carry the id end-to-end. No reshape of the existing middleware chain.
- Shipped `deploy/{prometheus,grafana,loki,tempo}/` configs + four dashboards (overview, latency, lifecycle, vault) + the alert rule catalog. Every push alert has a runbook URL annotation and a `for:` clause; tested against the YAML directly.
- 47 unit tests; all 10 testable Test Contract bullets pass; 4 bullets skipped against documented closure (2 cross-slice, 2 declared out-of-scope per work log). Branch coverage on owned code: 89% (gate 85%).
- One cross-slice ticket opened: `slice-ops-observability-slice-lifecycle-job-emit.md` — lifecycle workers should wrap their tick body in the `musubi_lifecycle_job_duration_seconds` + `musubi_lifecycle_job_errors_total` instruments shipped here. Slice-sdk-py-otel-spans.md (existing ticket from slice-sdk-py) covers the OTel deferral.
- Handoff checks: `make check` green (870 passed, 226 skipped), `make tc-coverage SLICE=slice-ops-observability` reports closure satisfied, `make agent-check` clean (warnings only — none touching this slice; the two ⚠ are about other slices flapping in parallel).
- Flipping `status: in-review`, marking PR ready, removing the lock.

### Known gaps at in-review

- **OTel deferred** to slice-sdk-py-otel-spans.md (existing cross-slice ticket). Spec § Tracing describes the full Tempo wiring; the deploy/tempo/ config + dashboard panels exist as scaffolding so the compose stack stands up, but the SDK doesn't emit spans yet.
- **Lifecycle worker emit deferred** to slice-ops-observability-slice-lifecycle-job-emit.md (new). The metrics + dashboards + email alert (`lifecycle_job_failing`) all reference the `musubi_lifecycle_job_*` family; the workers (in slice-lifecycle-* paths, forbidden here) need to wrap their ticks in the instrument-emit pattern shown in the ticket.
- **No real backfill of `musubi_capture_total` / `musubi_retrieve_total` / `musubi_vault_*`.** The middleware emits `musubi_http_requests_total` + `musubi_http_request_duration_ms` + `musubi_5xx_total` from real traffic; the planes / retrieve / vault families need the same instrument-emit treatment in their respective slices (already-shipped) — same pattern as the lifecycle ticket. Could be one consolidated cross-slice ticket if desired; left as-is for now since the dashboards explicitly call those metrics out and the absence is a noisy "no data" hint.

## Cross-slice tickets opened by this slice

- [[_inbox/cross-slice/slice-ops-observability-slice-lifecycle-job-emit|slice-ops-observability-slice-lifecycle-job-emit]] — lifecycle workers wrap their tick body in `musubi_lifecycle_job_*` + ledger emit; unskips Test Contract bullet 7.

## PR links

- [#104](https://github.com/ericmey/musubi/pull/104) — `slice/slice-ops-observability` → `v2`.
