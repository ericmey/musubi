---
title: "Agent Rules — Operations (09)"
section: 09-operations
type: index
status: complete
tags: [section/operations, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: true
---

# Agent Rules — Operations (09)

Local rules for `src/musubi/observability/`, `src/musubi/ops/`, `deploy/prometheus/`, and
every page under `09-operations/`. Supplements [[CLAUDE]].

## Must

- **Distinguish canonical vs derived assets.** See [[09-operations/asset-matrix]].
  Canonical assets (vault, artifact blobs, episodic memories, concepts, thoughts) need
  an off-host backup. Derived assets (model caches) are rebuildable.
- **Runbooks are copy-paste commands, not prose.** Step 1 is a shell command; step 2 is a
  check; step 3 is a branch. Format in [[_templates/runbook]].
- **Every recommended alert has a runbook.** An alert with no linked runbook is a bug in
  the alert.
- **Test restores.** `RESTORE_DRILL_CADENCE_DAYS` in `src/musubi/ops/backup.py` is 90:
  restore a backup set into a scratch environment at least that often and smoke-test it.
  A backup not tested is a backup not trusted.
- **Request ID on every API log line.** Core sets `request_id` from `X-Request-Id` (or
  mints one) for every request.
- **Document only what the code does.** A metric, log key, CLI command or schedule named
  on these pages must exist in `src/musubi`; mark anything else "planned, not
  implemented".

## Must not

- Ship a metric without saying, on [[09-operations/observability]], what it is for.
- Silently change the log line shape. Operators' log queries depend on the keys.
- Present `deploy/backup/restore.yml` or `drill.yml` as a working recovery path. They do
  not work today; see [[09-operations/backup-restore]].

## Observability stack

Musubi exposes telemetry; collection, dashboards, log aggregation, trace storage and
alerting are the operator's choice and live outside this repo.

- **Metrics:** Core serves Prometheus text at `GET /v1/ops/metrics`; the lifecycle worker
  serves its own at `:8101/metrics` on the Compose network. No Prometheus is bundled with
  the public stack; scrape with your own.
- **Logs:** structured JSON lines to container output. Ship them with whatever collector
  your host runs.
- **Traces:** OpenTelemetry via `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP/gRPC; empty disables
  tracing). No collector is bundled.
- **Dashboards and alert rules:** not shipped. [[09-operations/alerts]] lists recommended
  rules on the metrics Core actually emits.

## Incident response

When an alert fires:

1. Open the runbook linked from the alert.
2. Follow the numbered steps. If a step fails unexpectedly, stop and record it on the
   incident's issue.
3. If a runbook step is wrong or missing, fix it in the same PR as the incident
   write-up.
