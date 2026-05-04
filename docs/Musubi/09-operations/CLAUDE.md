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

Per [[13-decisions/0033-centralize-observability-on-shiori]], visualization /
log aggregation / trace storage / alerting all live on a dedicated
observability host (shiori) external to this repo. Local on the musubi host:

- **Metrics (local scrape):** Prometheus on the musubi compose bridge — scrapes `core:8100/v1/ops/metrics`, the three TEI services, node-exporter, and itself. Config at `deploy/prometheus/prometheus.yml`. Local TSDB retains 30 days for direct PromQL access at `127.0.0.1:9090` if shiori is unreachable.
- **Metrics (central forward):** prometheus `remote_write` → `shiori.mey.house:9009/api/v1/push` (Mimir). Source of truth for visualization, alerting, and multi-host correlation.
- **Host metrics:** node-exporter sidecar container; standard prom/node-exporter image, mounts /proc, /sys, / read-only.
- **Logs:** structured JSON to stdout (still required) — central Loki ingest from musubi is a follow-up PR scoped to the shiori-side codebase.
- **Traces:** OpenTelemetry instrumentation lands when needed; central Tempo on shiori receives them. No local trace collector planned.
- **Dashboards:** live on shiori (`http://shiori:3000`). Musubi-specific dashboards belong in the shiori-side codebase under `wiki/services/observability/dashboards/` (operator vault), not this repo.
- **Alerts:** central, configured against shiori Mimir/Grafana. Rule definitions live with the dashboards on the shiori side.

## Incident response

When an alert fires:

1. Open the runbook linked from the alert.
2. Follow the numbered steps. If a step fails unexpectedly, stop and file `_inbox/questions/<slice-id>-incident-<date>.md`.
3. Document the incident in `09-operations/incidents/<YYYY-MM-DD>-<slug>.md` (create if first of the day).
4. If a runbook step is wrong or missing, fix it **in the same PR as the incident write-up**.

## Related slices

- [[_slices/slice-ops-observability]], [[_slices/slice-ops-backup]].
