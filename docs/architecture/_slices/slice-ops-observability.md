---
title: "Slice: Metrics / logs / traces"
slice_id: slice-ops-observability
section: _slices
type: slice
status: in-progress
owner: vscode-cc-sonnet47
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-ops-compose]]"]
blocks: []
---

# Slice: Metrics / logs / traces

> Structured logs (Loki), metrics (Prometheus), traces (Tempo). Alert rules and dashboards ship in repo.

**Phase:** 8 Ops · **Status:** `in-progress` · **Owner:** `vscode-cc-sonnet47`

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

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
