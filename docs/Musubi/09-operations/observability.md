---
title: Observability
section: 09-operations
tags: [logs, metrics, operations, section/operations, status/complete, tracing, type/runbook]
type: runbook
status: complete
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: false
---
# Observability

Metrics, logs, traces. Designed to answer "is Musubi healthy, and if not, where is the problem?" in under a minute from a cold start.

## Metrics

### Stack

- **Collection:** Prometheus (scrapes every service's `/metrics`).
- **Storage:** Prometheus local TSDB (single-host, 30-day retention).
- **Visualization:** Grafana (self-hosted, same host).
- **Alerting:** Alertmanager → email / ntfy push.

All of this runs on the musubi host itself. It's a dedicated box; we don't need a separate monitoring host for v1.

### Core metrics

Musubi Core emits (standard `musubi_*` prefix):

**Capture:**

- `musubi_capture_total{namespace=...,plane=...}` counter
- `musubi_capture_duration_ms{...}` histogram
- `musubi_capture_dedup_total{action=merged|new}` counter
- `musubi_capture_rejected_total{reason=scope|validation|rate_limit}` counter

**Retrieve:**

- `musubi_retrieve_total{mode=fast|deep,plane=...}` counter
- `musubi_retrieve_duration_ms{mode=...}` histogram (p50/p95/p99 computed server-side)
- `musubi_retrieve_rerank_applied_total` counter
- `musubi_retrieve_result_count{mode=...}` histogram

**Lifecycle:**

- `musubi_lifecycle_job_duration_seconds{job=...}` histogram
- `musubi_lifecycle_job_errors_total{job=...}` counter
- `musubi_lifecycle_events_total{from_state=...,to_state=...}` counter
- `musubi_promotion_total{result=success|gate_failed|llm_failed}` counter

**Vault:**

- `musubi_vault_sync_latency_ms` histogram (vault event → qdrant update)
- `musubi_vault_echo_filtered_total` counter
- `musubi_vault_events_total{kind=created|modified|moved|deleted}` counter
- `musubi_vault_reconcile_diff_count` gauge (last run)

**Errors:**

- `musubi_errors_total{code=BAD_REQUEST|FORBIDDEN|...}` counter
- `musubi_5xx_total{endpoint=...}` counter

### Inference metrics

From [[08-deployment/gpu-inference-topology]]:

- `tei_request_duration_ms{service=dense|sparse|reranker}` histogram
- `ollama_generation_ms{purpose=synthesis|rendering|maturation}` histogram
- `gpu_vram_used_mb` gauge (sidecar scraper)
- `gpu_utilization_pct` gauge

### Qdrant metrics

Scraped from `/metrics`:

- `qdrant_points_count{collection=...}`
- `qdrant_grpc_requests_duration_seconds{endpoint=...}`
- `qdrant_segments_count{collection=...}` (high = needs optimizer)

### Host metrics

- `node_filesystem_avail_bytes{mountpoint=/var/lib/musubi}`
- `node_memory_MemAvailable_bytes`
- `node_cpu_seconds_total`

Standard `node_exporter`.

## Logs

### Format

Structured JSON. Every line:

```json
{
  "ts": "2026-04-17T10:21:34.512Z",
  "level": "info",
  "service": "core",
  "request_id": "abc-123",
  "namespace": "eric/claude-code/episodic",
  "event": "memory.captured",
  "object_id": "k1a2b3c...",
  "duration_ms": 42,
  "msg": "captured memory"
}
```

`request_id` is present on every API-scoped log line. Lifecycle jobs use `job_id` instead.

### Transport

Containers log to journald (see [[08-deployment/compose-stack]]). Core also writes to `/var/log/musubi/core.log` rotated daily — easier to grep than journald for quick investigation.

### Centralized log search

For v1: `grep` over the log files. Good enough for one host.

Post-v1: Loki (runs co-resident with Grafana). Drop-in.

### What to log at what level

| Level | Example |
|---|---|
| DEBUG | "qdrant query params: {...}" (off in prod) |
| INFO | "captured memory", "retrieved 5 results" |
| WARN | "LLM timeout on synthesis; skipping cluster" |
| ERROR | "failed to promote concept <id>; see exception" |
| CRITICAL | "qdrant unreachable for 60s" |

CRITICAL triggers alerts.

### Never log

- Token values.
- Full content of captured memories (tag/topic/object_id only, or first 60 chars for debugging).
- PII in any form.

## Tracing

### Stack

OpenTelemetry SDK in Core + adapter libraries. Spans go to a local **OTel Collector** → Tempo (or Jaeger) running on the host.

### Span hierarchy

```
root: POST /v1/retrieve                       120ms
├── auth.validate                             2ms
├── retrieve.orchestration                    115ms
│   ├── retrieve.dense_encode (TEI)           35ms
│   ├── retrieve.sparse_encode (TEI)          22ms
│   ├── retrieve.qdrant.query_points          40ms
│   ├── retrieve.rerank (TEI)                 15ms
│   └── retrieve.score.blend                  1ms
└── response.serialize                        3ms
```

Useful for latency regressions ("rerank went from 15ms to 80ms on April 15 — what changed?").

### Sampling

100% in v1 (low traffic; dedicated host has spare headroom). Post-v1: tail-based sampling retaining all errors + 10% of success.

## Dashboards

Grafana boards provisioned via JSON:

### `musubi-overview`

- Capture rate (per minute, stacked by plane)
- Retrieval rate (per minute, stacked by mode)
- Error rate (per minute, stacked by code)
- Recent promotions / demotions / contradictions count (last 24h)
- VRAM used vs total
- Disk used vs total on `/var/lib/musubi`

### `musubi-latency`

- Retrieve p50/p95/p99 (fast, deep)
- Capture p50/p95/p99
- Rerank p95
- Ollama generation p95

### `musubi-lifecycle`

- Job runtimes (stacked by job name)
- Job error rate
- Promotion queue length
- Concept attempts over time
- Contradictions unresolved (current gauge)

### `musubi-vault`

- Vault events per minute
- Sync latency p95
- Echo filter rate
- Reconcile diff count (over time)

## Request ID propagation

Every request gets an `X-Request-Id` at the Kong edge. Core echoes it in:

- Response headers.
- All log lines.
- OTel span attributes.
- Forwarded downstream to TEI / Ollama (custom `X-Musubi-Request-Id` header the SDK recognizes — though TEI/Ollama may not log it, Core's spans link correctly either way).

Adapter clients (MCP, LiveKit, OpenClaw) propagate the request-id, so an end-to-end trace can follow a single `memory_capture` call from the user's coding session through MCP → Musubi → Qdrant → TEI.

## Key graphs for common questions

**"Is retrieval fast?"**

Look at `musubi_retrieve_duration_ms` p95 by mode. Target: fast ≤ 400ms p95, deep ≤ 5s p95. If breaching, drill into spans.

**"Is the lifecycle keeping up?"**

Look at `musubi_lifecycle_job_duration_seconds` and `musubi_lifecycle_events_total`. If maturation runs > 30 min, there's a backlog.

**"Is the vault in sync?"**

`musubi_vault_sync_latency_ms` should be a few seconds p95. Spikes indicate watcher or echo-log issues.

**"Are we running out of resources?"**

- VRAM: `gpu_vram_used_mb` vs budget.
- Disk: `node_filesystem_avail_bytes` / total.
- RAM: `node_memory_MemAvailable_bytes`.

## What we don't build (yet)

- **Anomaly detection** — we use flat thresholds for v1.
- **SLO reports** — error budget tracking deferred; we have enough signal from raw metrics.
- **Multi-region** — single-host.
- **User-visible status page** — not needed at household scale.

## Test contract

**Module under test:** `musubi/observability/` + metrics exported

1. `test_every_endpoint_emits_request_counter`
2. `test_every_endpoint_emits_latency_histogram`
3. `test_errors_increment_errors_total`
4. `test_log_line_contains_request_id_for_api_calls`
5. `test_log_line_never_contains_raw_token` (grep test)
6. `test_otel_span_covers_retrieve_orchestration`
7. `test_lifecycle_job_start_end_emitted_to_events_table`
8. `test_dashboard_json_loads_in_grafana` (integration)
