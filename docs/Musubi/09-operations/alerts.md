---
title: Alerts
section: 09-operations
tags: [alerts, on-call, operations, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: false
---
# Alerts

What is worth a page, what is worth an email, and what stays on a dashboard. Aimed at a
small team with one operator: noise hurts more than it helps.

**Musubi ships no alert rules and no running Alertmanager.** This page is a recommended
rule set, written against the metrics Core and the lifecycle worker actually emit (see
[[09-operations/observability]]). Load the rules into whatever alerting you already run.
Some conditions need signals from outside Musubi (an uptime probe, a host exporter, your
backup tool); the tables say which.

## Routing

Two channels are enough:

1. **Push** (ntfy, Pushover, a pager): urgent, wake-the-operator.
2. **Email:** next-day.

`deploy/prometheus/alertmanager.yml` is a shape example only. Its `default` receiver
posts to `/v1/ops/alert-sink`, which Core does not serve, and its ntfy receiver names a
public topic. Replace both before using it. A minimal routing block:

```yaml
route:
  receiver: email
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 1h
  routes:
    - matchers: ['severity="push"']
      receiver: ntfy
      repeat_interval: 30m
    - matchers: ['severity="email"']
      receiver: email
      repeat_interval: 24h

receivers:
  - name: ntfy
    webhook_configs:
      - url: "https://ntfy.sh/<your-topic>"
  - name: email
    email_configs:
      - to: admin@example.com
        from: alertmanager@example.com
        smarthost: smtp.example.com:587
```

Use an unguessable ntfy topic, or your own ntfy server: anyone who knows a public topic
name can read it.

## In-band alert: lifecycle job failures

Independently of Prometheus, when a lifecycle job tick fails, the lifecycle worker posts a
Thought on the `ops-alerts` channel (from presence `lifecycle-worker`, namespace
`lifecycle-worker/ops/thought`). If posting that Thought fails, the worker counts it in
`musubi_lifecycle_job_alert_errors_total`.

## Recommended catalog

In the queries below, `job="musubi-core"` and `job="musubi-lifecycle"` stand for whatever
scrape job names you give Core's `/v1/ops/metrics` and the worker's `:8101/metrics`.

### Push (urgent)

Fire these only when something is broken and worse if left until morning.

| Alert | Condition | Source | Runbook |
|---|---|---|---|
| `core_down` | `up{job="musubi-core"} == 0` for 2m | your Prometheus | [[09-operations/runbooks#core-down]] |
| `core_5xx_high` | `sum(rate(musubi_5xx_total[5m])) / sum(rate(musubi_http_requests_total[5m])) > 0.01` for 5m | Core | [[09-operations/runbooks#core-5xx-high]] |
| `qdrant_down` | `GET /v1/ops/status` reports `components.qdrant.healthy=false` for 2m | an HTTP probe (e.g. blackbox exporter) | [[09-operations/runbooks#qdrant-down]] |
| `lifecycle_worker_not_ready` | `musubi_lifecycle_coordinator_ready == 0` or absent for 10m | lifecycle worker | [[09-operations/runbooks#lifecycle-worker-not-ready]] |
| `vault_fs_full` | filesystem holding Docker's volumes < 10% free | a host exporter | [[09-operations/runbooks#vault-fs-full]] |
| `backup_failure_24h` | no successful cold backup in 24h | your backup tool | [[09-operations/runbooks#backup-failure-24h]] |
| `gpu_oom` | (GPU overlay) a TEI or Ollama container was OOM-killed or keeps restarting | container metrics or Docker events | [[09-operations/runbooks#gpu-oom]] |

### Email (next morning)

| Alert | Condition |
|---|---|
| `lifecycle_job_failing` | `increase(musubi_lifecycle_job_errors_total[3h]) >= 3`, per `job` |
| `lifecycle_alert_emit_failing` | `increase(musubi_lifecycle_job_alert_errors_total[1h]) > 0` |
| `lifecycle_outbox_backlog` | `musubi_lifecycle_outbox_pending` above a threshold you choose, for 1h |
| `retrieval_errors` | `sum(increase(musubi_retrieval_errors_total{kind=~"internal|timeout"}[15m])) > 0` |
| `retrieval_degraded` | `sum by (warning) (increase(musubi_retrieval_warnings_total[1h]))` above a threshold you choose |
| `retrieve_latency_high` | p95 of `musubi_http_request_duration_ms{endpoint="/v1/retrieve"}` > 500 ms for 15m |
| `disk_growth_anomaly` | the volumes' filesystem grew > 2x its usual weekly rate (host exporter) |

p95 query for the latency rule:

```promql
histogram_quantile(0.95,
  sum by (le) (rate(musubi_http_request_duration_ms_bucket{endpoint="/v1/retrieve"}[5m])))
```

### Dashboard-only (not alerted)

- A single slow request (flap-prone; alert on aggregates only).
- `musubi_reranker_degradation_causes_total` by `cause` (explains `retrieval_degraded`).
- `musubi_lifecycle_job_duration_seconds` by `job`.
- `musubi_retention_deleted_total`, `musubi_cleanup_deleted_total` (zero unless you run
  those workers yourself).

### Not alertable today (planned, not implemented)

These need signals Musubi does not emit yet: vault write-echo loops, stale promotions,
unresolved contradictions, provisional-memory backlog, and evaluation regressions.

## Thresholds: how they were chosen

**Core 5xx > 1% for 5m:** at ~100 req/min, 1% is one error a minute; five minutes of
that is real signal with few false positives.

**Qdrant down 2m:** long enough to ride out a restart and WAL replay, short enough to act
before clients pile up retries.

**Lifecycle worker not ready 10m:** the worker's readiness covers its shared lifecycle
storage; a short blip during restart is normal.

**Backup failure 24h:** with a daily cold backup, one missed run means the schedule is
broken.

## Suppression

During planned maintenance, silence alerts in your alerting tool, with a comment and an
expiry. With Alertmanager:

```bash
amtool silence add alertname=~"core_down|core_5xx_high|qdrant_down" \
  --duration=1h --comment="planned compose update"
```

## Runbook linkage

Every push alert above has a section in [[09-operations/runbooks]]. Put the runbook link
in each rule's annotations. An alert without a runbook should not fire.

## Testing alerts

Once a quarter, on a non-production stack:

- Stop Qdrant (`docker compose stop qdrant`): expect `qdrant_down` within ~2m.
- Stop Core: expect `core_down`.
- Stop the lifecycle worker: expect `lifecycle_worker_not_ready` (the metric goes absent).

Record expected and actual behaviour. If an alert doesn't fire, fix the rule and re-run.

## On-call model

One operator. When they are unavailable, alerts buffer in the push channel; escalate to
email after 30 minutes un-acked. There is no automatic failover of on-call.

## Test contract

1. `test_alertmanager_config_loads_without_error` (`tests/observability/test_alerts.py`)
   — the example Alertmanager file parses and has the `ntfy` and `email` receivers.
2. `test_alertmanager_routes_push_severity_to_ntfy`
3. `test_alertmanager_routes_email_severity_to_email`
4. `test_chaos_drill_qdrant_down_fires_within_3m` — **skipped**; needs a live
   Prometheus + Alertmanager loop.

No test checks the recommended rules on this page; they are not shipped.
