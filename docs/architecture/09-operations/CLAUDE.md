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

Local rules for `musubi/observability/`, `musubi/ops/`, `deploy/grafana/`, `deploy/loki/`, and every runbook under `09-operations/`. Supplements [[CLAUDE]].

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

- **Metrics:** Prometheus scrape from `musubi-core:9100` + `qdrant:6333/metrics`.
- **Logs:** structured JSON → Loki.
- **Traces:** OpenTelemetry → Tempo (optional in v1, required by v1.5).
- **Dashboards:** Grafana; committed to `deploy/grafana/dashboards/`.
- **Alerts:** Grafana + Alertmanager → email/Pushover. Rules in `deploy/grafana/alerts/`.

## Incident response

When an alert fires:

1. Open the runbook linked from the alert.
2. Follow the numbered steps. If a step fails unexpectedly, stop and file `_inbox/questions/<slice-id>-incident-<date>.md`.
3. Document the incident in `09-operations/incidents/<YYYY-MM-DD>-<slug>.md` (create if first of the day).
4. If a runbook step is wrong or missing, fix it **in the same PR as the incident write-up**.

## Related slices

- [[_slices/slice-ops-observability]], [[_slices/slice-ops-backup]].
