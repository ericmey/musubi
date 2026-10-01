---
title: Observability
section: 09-operations
tags: [logs, metrics, operations, section/operations, status/complete, tracing, type/runbook]
type: runbook
status: complete
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: false
---
# Observability

Metrics, logs, traces: what Musubi emits, and where to find it. Musubi only exposes
telemetry. Collection, storage, dashboards and alerting (Prometheus, Grafana,
Alertmanager, a log store, a trace store, or a hosted equivalent) are the operator's
choice and are not bundled with the public stack.

## Metrics

### Where to scrape

| Endpoint | Process | Reachable at |
|---|---|---|
| `GET /v1/ops/metrics` | Core | Core's published port (`127.0.0.1:8100` by default) |
| `GET /metrics` on port 8101 | lifecycle worker | Compose network only (`lifecycle-worker:8101`) |

Both serve Prometheus text format and need no bearer token
([[13-decisions/0038-network-protect-read-only-ops-endpoints]]). To scrape the worker, run
your Prometheus (or agent) on the stack's Compose network. Each process has its own
registry, and a metric appears on whichever process loads the module that defines it, so
scrape both. The job-tick metrics below come only from the worker.

### Core (`/v1/ops/metrics`)

**HTTP** (`src/musubi/observability/metrics_middleware.py`). `endpoint` is the route
template (e.g. `/v1/retrieve`, `/v1/episodic/{object_id}`), or `<unmatched>`; requests to
the metrics path itself are not counted.

- `musubi_http_requests_total{endpoint,method,status}` counter
- `musubi_http_request_duration_ms{endpoint,method}` histogram (buckets 5 ms to 5 s)
- `musubi_5xx_total{endpoint}` counter

**Retrieval** (`src/musubi/observability/retrieval_metrics.py`):

- `musubi_retrieval_warnings_total{warning,plane}` counter: degraded but successful
  retrievals (e.g. `sparse_embedding_failed`, `reranker_failed`)
- `musubi_retrieval_errors_total{kind}` counter: failed retrievals; `kind` is
  `bad_query`, `forbidden`, `timeout` or `internal`
- `musubi_reranker_degradation_causes_total{cause,plane}` counter

**Durability failures** (counters that should stay at zero):

- `musubi_idempotency_store_failures_total`
- `musubi_idempotency_receipt_store_failures_total`

### Lifecycle (mainly the worker's `:8101/metrics`)

- `musubi_lifecycle_job_duration_seconds{job}` histogram (per tick)
- `musubi_lifecycle_job_errors_total{job}` counter
- `musubi_lifecycle_job_alert_errors_total{job}` counter (failed to post the
  `ops-alerts` Thought)
- `musubi_lifecycle_coordinator_ready` gauge (`1` when the worker's lifecycle storage is
  open; the container health check reads it)
- `musubi_lifecycle_outbox_pending` gauge (non-terminal lifecycle outbox depth)
- `musubi_lifecycle_outbox_mutation_failures_total`,
  `musubi_lifecycle_event_write_failures_total`,
  `musubi_lifecycle_enrichment_batch_failures_total`,
  `musubi_lifecycle_synthesis_family_failures_total`,
  `musubi_lifecycle_synthesis_decode_skips_total` counters

### Maintenance workers

`musubi_retention_deleted_total{plane}` and `musubi_cleanup_deleted_total{collection}` are
defined in `src/musubi/ops/retention.py` and `src/musubi/ops/cleanup.py`. Nothing in the
public stack runs those workers, so expect them to be absent.

### What Musubi does not emit

No per-plane capture counters, no vault-sync metrics, no TEI, Ollama or GPU metrics, no
host metrics. For those, use exporters you choose (an NVIDIA GPU exporter, a host
exporter, Qdrant's own `/metrics` on the Compose network, which needs the API key).

## Logs

### Format

Core and the lifecycle worker write one JSON object per line to container output
(`docker compose logs core`, `docker compose logs lifecycle-worker`). Shape
(`src/musubi/observability/logging_setup.py`):

```json
{
  "ts": "2026-04-17T10:21:34.512Z",
  "level": "info",
  "service": "musubi.api.app",
  "msg": "…",
  "request_id": "3f1c…",
  "trace_id": "…",
  "span_id": "…"
}
```

- `ts`, `level`, `service` (the Python logger name) and `msg` are always present.
- `request_id` appears on lines logged while serving a request.
- `trace_id` / `span_id` appear when tracing is enabled and a span is active.
- Any extra fields the caller attaches (for example `namespace`, `object_id`) are added at
  the top level.

JWT-shaped strings are redacted before a line is written.

### Transport

There is no file handler: logs go to the container runtime only. `LOG_DIR` and the `logs`
volume exist but nothing writes there today. Retention and shipping are set by your Docker
logging driver and whatever log collector you run.

### What to log at what level

| Level | Example |
|---|---|
| DEBUG | query parameters (off in production) |
| INFO | "captured memory", job completions |
| WARNING | an LLM timeout during synthesis; a cluster skipped |
| ERROR | a failed promotion, with the exception |

### Never log

- Token values.
- Full content of captured memories.
- Personal data beyond identifiers.

## Tracing

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to an OTLP/gRPC endpoint to export spans; empty (the
default) disables tracing. No collector is bundled. Related settings:
`OTEL_SERVICE_NAME` (default `musubi-core`), plus service namespace, environment and host
attributes in `src/musubi/settings.py`.

What gets spans:

- every FastAPI request (auto-instrumentation);
- `retrieve.orchestration` around retrieval;
- `lifecycle.job.<name>` around each lifecycle job tick.

No sampler is configured in code, so the OpenTelemetry SDK default applies.

## Dashboards

None are shipped. A useful starting set, built against the metrics above:

- **Overview:** request rate by endpoint, 5xx ratio, retrieval error and warning rates,
  `/v1/ops/status` probe result.
- **Latency:** p50/p95/p99 of `musubi_http_request_duration_ms` for `/v1/retrieve` and
  the capture endpoints.
- **Lifecycle:** job durations and errors by `job`, outbox depth, coordinator readiness.

## Request ID propagation

Core reads `X-Request-Id` from the request, or mints a UUID if it is missing, puts it on
every log line for that request, and echoes it on the response. The Python SDK in
`src/musubi/sdk` sends `X-Request-Id` when the caller supplies one. Core does not forward
it to TEI or Ollama. Whether each external adapter repository propagates it is unverified.

## Key questions

**"Is retrieval fast?"** p95 of `musubi_http_request_duration_ms{endpoint="/v1/retrieve"}`.
If it is breaching, look at traces for `retrieve.orchestration`.

**"Is retrieval degraded?"** `musubi_retrieval_warnings_total` by `warning`, and
`musubi_retrieval_errors_total` by `kind`.

**"Is the lifecycle keeping up?"** `musubi_lifecycle_job_duration_seconds` and
`musubi_lifecycle_job_errors_total` by `job`; `musubi_lifecycle_outbox_pending`.

**"Is everything up?"** `GET /v1/ops/status`.

## Test contract

**Module under test:** `src/musubi/observability/` (`tests/observability/test_observability.py`).

1. `test_every_endpoint_emits_request_counter`
2. `test_every_endpoint_emits_latency_histogram`
3. `test_errors_increment_errors_total`
4. `test_log_line_contains_request_id_for_api_calls`
5. `test_log_line_never_contains_raw_token`
6. `test_otel_span_covers_retrieve_orchestration`
7. `test_lifecycle_job_start_end_emitted_to_events_table` — currently fails when its file
   runs alone: it imports the job modules, but the job metrics are now registered in
   `musubi.lifecycle.runner`.
8. `test_dashboard_json_loads_in_grafana` — **skipped**; no dashboards are shipped.
