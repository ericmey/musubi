---
title: Process Topology
section: 03-system-design
tags: [architecture, processes, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/architecture/03-system-design/"
---
# Process Topology

How processes map to containers, which talk to which, restart / crash behavior, and resource envelopes.

## Process inventory

| # | Container | Image | Role | Exposes | Mounts |
|---|---|---|---|---|---|
| 1 | `musubi-core` | built local | API server | `:8100` HTTP, `:8101` gRPC (both on Docker net only; edge TLS via Kong) | `/srv/musubi/vault:ro`, `/srv/musubi/artifacts` |
| 2 | `musubi-lifecycle` | same as core | Background jobs | `:9100` /metrics, /healthz | `/srv/musubi/vault`, `/srv/musubi/artifacts`, `/srv/musubi/lifecycle-state` |
| 3 | `musubi-vault-watcher` | same as core | Vault reindexer | `:9101` /metrics, /healthz | `/srv/musubi/vault` |
| 4 | `musubi-qdrant` | `qdrant/qdrant:v1.15+` | Vector DB | `:6333`, `:6334` (Docker net only) | `/srv/musubi/qdrant/storage` |
| 5 | `musubi-tei` | `ghcr.io/huggingface/text-embeddings-inference:1.7-gpu-cuda13` | Embeddings + reranker | `:8080` (Docker net only) | `/srv/musubi/models:ro` (HF cache) |
| 6 | `musubi-ollama` | `ollama/ollama:latest` | Local LLM | `:11434` (Docker net only) | `/srv/musubi/ollama` |
| 7 | `kong` | `(Kong image; managed outside this repo)` | TLS edge proxy | `:80`, `:443` | Kong route config, certs |
| 8 | `musubi-prometheus` (opt) | `prom/prometheus` | Metrics | `:9090` | prom data |
| 9 | `musubi-grafana` (opt) | `grafana/grafana` | Dashboards | `:3000` | grafana data |

All containers share a Docker user-defined network (`musubi`). Only Kong (and optionally Grafana) binds to host.

## Resource envelopes (reference host: Ryzen 5 / 32GB / RTX 3080 10GB)

| Container | CPU (cores) | RAM | VRAM | Disk (growth/year) |
|---|---|---|---|---|
| musubi-core | 1–2 | 1–2 GB | 0 | logs: 5 GB |
| musubi-lifecycle | 1–2 | 2–4 GB (synth bursts) | 0 (calls Ollama) | 500 MB |
| musubi-vault-watcher | 0.5 | 500 MB | 0 | - |
| musubi-qdrant | 2–4 | 4–8 GB | 0 | 20–50 GB (10M points) |
| musubi-tei | 1 | 2 GB | ~5 GB (pinned) | 3 GB model cache |
| musubi-ollama | 1 (peak) | 2 GB | 5–6 GB (on demand) | 5 GB model cache |
| kong | 0.1 | 100 MB | 0 | - |
| prom + grafana | 0.5 | 500 MB | 0 | 2 GB |
| **Totals (peak)** | ~10 of 12 | ~20 of 32 | ~10 of 10 | ~80 GB |

**VRAM note**: TEI pins ~5 GB; Ollama uses 5–6 GB on demand. TEI has its `-e POOL_STRATEGY=cls` and `-e MAX_CONCURRENT_REQUESTS=4` set to bound peak allocation. Ollama has `OLLAMA_KEEP_ALIVE=5m` so the 7B Q4 model unloads between synthesis runs, leaving VRAM for TEI. See [[08-deployment/gpu-inference-topology]] for the exact budget and concurrency plan.

## Startup order

Ansible and Docker Compose `depends_on` enforce this order:

1. `kong` (immediately; idles until upstream is ready, serves 502 meanwhile).
2. `musubi-qdrant` — no deps. Must pass healthcheck before 3.
3. `musubi-tei` — no deps except GPU. Must pass healthcheck (model loaded) before 4.
4. `musubi-ollama` — no deps except GPU. Pulls models if missing.
5. `musubi-core` — depends on qdrant + tei healthchecks. Needs ollama only lazily.
6. `musubi-lifecycle` — depends on core healthy.
7. `musubi-vault-watcher` — depends on core healthy.
8. (optional) prometheus/grafana — last.

## Restart policies

- `kong`: `unless-stopped` — never auto-restart on config error (human must fix).
- `musubi-core`, `musubi-lifecycle`, `musubi-vault-watcher`: `on-failure:5` — five restart attempts with exponential backoff, then stop and alert.
- `musubi-qdrant`: `unless-stopped` — critical; always try to come back.
- `musubi-tei`, `musubi-ollama`: `unless-stopped` — GPU failures often recover on restart.

## Healthchecks

Every service has `/healthz` (liveness) and `/readyz` (readiness, checks deps).

- `musubi-core /readyz`: verifies Qdrant reachable, vault mount writable, TEI reachable.
- `musubi-lifecycle /readyz`: verifies Qdrant, vault mount, TEI, Ollama (all reachable — ollama optional).
- `musubi-vault-watcher /readyz`: verifies vault mount readable and watcher thread alive.

Kong routes `/` only to ready-core instances. If Core is unready, Kong returns 503 with a Retry-After header.

## Crash blast radius

| Crash | Impact |
|---|---|
| musubi-core | Read/write API unavailable. Lifecycle worker continues. Vault watcher continues reindexing (writes sit in a local queue, drained on Core restart). |
| musubi-lifecycle | No maturation/synthesis runs. No user-facing impact. Will catch up on restart. |
| musubi-vault-watcher | Vault edits don't reindex. Humans can still read curated via stale index. On restart, watcher does an initial reconcile pass (hash-based; see [[06-ingestion/vault-sync]]). |
| musubi-qdrant | All retrieval 503s. Core returns clean errors. Restart is typically < 30s. |
| musubi-tei | Ingestion 503s (can't embed). Reranker unavailable → scorer falls back to pure hybrid without rerank. |
| musubi-ollama | Importance scoring + synthesis stalled. Non-critical. |
| kong | External reachability broken. Internal containers continue. |

## Logging and observability

- All services log structured JSON to stdout → captured by Docker's `json-file` driver with rotation.
- Ansible deploys a `promtail` DaemonSet-equivalent (just a single container) if log aggregation is enabled (optional).
- All services expose `/metrics` in Prometheus format.
- Correlation IDs propagate via `X-Musubi-Request-Id` header.
- See [[09-operations/observability]].

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the per-component slices listed in the sibling specs under this section, each of which carries its own `## Test Contract` section bound to an owning slice.
