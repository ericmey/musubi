---
title: "Agent Rules — Operations (09)"
section: 09-operations
type: index
status: complete
tags: [section/operations, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: true
---

# Agent Rules — Operations (09)

Local rules for `musubi/observability/`, `musubi/ops/`, `deploy/prometheus/`, and every runbook under `09-operations/`. Supplements [[CLAUDE]].

## Must

- **Distinguish canonical vs derived assets.** See [[09-operations/asset-matrix]]. Canonical assets (vault, artifact blobs) require offsite backup. Derived assets (Qdrant collections, TEI model caches) are rebuildable.
- **Runbooks are copy-paste commands, not prose.** Step 1 is a shell command; step 2 is a check; step 3 is a branch. Format enforced in [[_templates/runbook]].
- **Every alert has a runbook.** A fired alert with no linked runbook is a bug in the alert.
- **Snapshots tested weekly.** Restore into a scratch environment; smoke-test retrieval. A snapshot not tested is a snapshot not backed up.
- **Correlation ID on every log line.** Requests and jobs both.

## Must not

- Delete a snapshot older than 30 days (retention policy).
- Ship a metric without a dashboard or alert tied to it. Orphan metrics rot.
- Silently change a log schema — downstream Loki queries break. Version log schemas.

## Observability stack

Musubi exposes telemetry; visualization, log aggregation, trace storage and alerting are the
operator's choice and live outside this repo (the original deployment's decision is recorded in
[[13-decisions/0033-centralize-observability-on-shiori]]). On the Musubi host:

- **Metrics (local scrape):** Prometheus on the musubi compose bridge scrapes `core:8100/v1/ops/metrics`, qdrant (Bearer-authed `/metrics`), the TEI services, node-exporter, and itself. Config rendered from `deploy/ansible/templates/prometheus.yml.j2`. The local TSDB retains 30 days for direct PromQL at `127.0.0.1:9090`.
- **Metrics (central forward):** optional Prometheus `remote_write` to your metrics backend (Mimir, Cortex, Thanos, a hosted service).
- **Host metrics:** node-exporter sidecar container; standard prom/node-exporter image, mounts /proc, /sys, / read-only.
- **Logs:** structured JSON to stdout (required). Ship them with whatever collector your host runs.
- **Traces:** OpenTelemetry via `OTEL_EXPORTER_OTLP_ENDPOINT` (empty disables tracing). No local trace collector is bundled.
- **Dashboards and alerts:** built against your backend; they are not shipped in this repo.

## Incident response

When an alert fires:

1. Open the runbook linked from the alert.
2. Follow the numbered steps. If a step fails unexpectedly, stop and file `_inbox/questions/<slice-id>-incident-<date>.md`.
3. Document the incident in `09-operations/incidents/<YYYY-MM-DD>-<slug>.md` (create if first of the day).
4. If a runbook step is wrong or missing, fix it **in the same PR as the incident write-up**.

## Related slices

- [[_slices/slice-ops-observability]], [[_slices/slice-ops-backup]].
