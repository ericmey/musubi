---
title: "Slice: Metrics / logs / traces"
slice_id: slice-ops-observability
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "8 Ops"
tags: [section/slices, status/done, type/slice]
updated: 2026-05-14
reviewed: true
depends-on: ["[[_slices/slice-ops-compose]]"]
blocks: ["[[_slices/slice-ops-first-deploy]]", "[[_slices/slice-ops-hardening-suite]]"]
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
- ~~`deploy/grafana/`~~                              (planned, never built — superseded by [[13-decisions/0033-centralize-observability-on-shiori]])
- ~~`deploy/loki/`~~                                 (planned, never built — superseded by [[13-decisions/0033-centralize-observability-on-shiori]])
- `deploy/prometheus/`                               (new — prom config + scrape jobs)
- ~~`deploy/tempo/`~~                                (planned, never built — superseded by [[13-decisions/0033-centralize-observability-on-shiori]])
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

### 2026-05-03 — aoi/command-chair — partial supersession by ADR 0033

- The local Grafana / Loki / Tempo stack this slice planned to deploy was never actually built. Only `deploy/prometheus/` made it from the `## Owned paths` list into the production compose template; `deploy/grafana/`, `deploy/loki/`, `deploy/tempo/` existed as scaffolding but never ran.
- A dedicated observability host (shiori) now provides the LGTM stack centrally. [[13-decisions/0033-centralize-observability-on-shiori]] formalizes the supersession: musubi keeps prometheus locally as a scraper, adds node-exporter for host metrics, and `remote_write`s everything to shiori's Mimir.
- This work-log entry retains the slice's history; the slice is not reopened (status stays `done`). The superseded `## Owned paths` entries are struck through with a wikilink to ADR 0033 for traceability.
- Future central-side artifacts (musubi-specific dashboards on shiori, alert routing, log shipping) are out of this slice's scope per ADR 0033 — they belong to follow-up slices/PRs scoped to the shiori-side codebase.

### 2026-05-13 — aoi/command-chair — Tracing section was never implemented

While building the harem-ops fleet telemetry coverage map, discovered that
the **Tracing portion** of this slice's spec
(`09-operations/observability.md` § Tracing — "OpenTelemetry SDK in Core +
adapter libraries. Spans go to a local OTel Collector → Tempo") was
**never implemented**. The slice closed marking observability complete,
but only metrics + the `StructuredJsonFormatter` shipped. In
production code (`src/musubi/`), there are no OTel imports outside
`src/musubi/sdk/tracing.py` (the SDK consumer-side, which is itself
dormant). Test modules import the OTel SDK to exercise SDK behaviour,
but no shipping code path emits spans.

Verified by querying the live Tempo datasource on shiori via the Grafana
MCP: zero tags, zero `service.name` values, zero traces. The whole fleet
has zero trace coverage.

The status of this slice **stays `done`** — its history is the artifact.
The unshipped Tracing scope is being completed under issue
[#302](https://github.com/ericmey/musubi/issues/302) without expanding
beyond the original spec. Two small companion fixes are included to make
the spec actually true:

- Route uvicorn access + error logs through `StructuredJsonFormatter`
  (currently they bypass it and emit plain `INFO: ... GET ...` lines —
  the JSON-logs contract the spec mandated has not been honoured in
  practice).
- Inject `trace_id` / `span_id` into the formatter's payload when there
  is an active span — connecting tissue between the two telemetry
  signals the spec already requires.

This is debt repayment, not a new slice.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- [#303](https://github.com/ericmey/musubi/pull/303) — server-side OTel
  SDK init, FastAPI auto-instrumentation, `retrieve.orchestration` hand-
  rolled span, uvicorn-through-StructuredJsonFormatter, formatter
  promotes `otelTraceID`/`otelSpanID` → top-level `trace_id`/`span_id`.
- [#306](https://github.com/ericmey/musubi/pull/306) — wires
  `OTEL_EXPORTER_OTLP_ENDPOINT` + `MUSUBI_SERVICE_VERSION` into
  `.env.production` from new ansible group_vars defaults; extends the
  `auto-digest-bump.yml` generator so `musubi_core_version` is rewritten
  in lockstep with `musubi_core_image` on every release-please pin PR.

## Deploy state — 2026-05-14

- v1.3.2 image rolled to musubi-workload via
  `deploy/ansible/update.yml` (recreated `core` + `lifecycle-worker`,
  fixing the latter's 2-week drift from v1.3.1's roll).
- `.env.production` refreshed via `deploy/ansible/config.yml`; the
  running container exposes
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://shiori.mey.house:4317` and
  `MUSUBI_SERVICE_VERSION=v1.3.2`.
- Spans verified flowing to Tempo on shiori: `GET /v1/ops/status`,
  `/v1/ops/health`, `/v1/ops/metrics` all land with
  `rootServiceName=musubi-core`.

### Known follow-up

Emitted log lines in Loki don't yet carry `trace_id`/`span_id` despite
LoggingInstrumentor being part of the init path. Probable causes:
uvicorn access logs emit after the request span ends (context detached),
and outbound `httpx` calls aren't auto-instrumented
(`opentelemetry-instrumentation-httpx` is a separate package not in
`[otel]` extras). Tempo↔Loki correlation jumps therefore fall back to
`service.name + time-window` rather than a precise `trace_id` join.
Tracked separately; spans flow either way.
