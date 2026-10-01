---
title: Process Topology
section: 03-system-design
tags: [architecture, processes, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Process Topology

How processes map to containers, which talk to which, restart and crash behavior.

> The stack is defined in the root `docker-compose.yml`, with an optional GPU overlay in `deploy/docker/compose.local-gpu.yml`. See [[08-deployment/compose-stack]].

## Process inventory

Root Compose file (Compose project `musubi`):

| Service | Image | Role | Exposes | Volumes |
|---|---|---|---|---|
| `volume-init` | Core image, runs as root once | `chown`s the Musubi volume roots to UID 999 / GID 985, then exits | — | `vault`, `artifact-blobs`, `lifecycle`, `logs` |
| `qdrant` | `qdrant/qdrant:v1.17.1` (digest-pinned) | Vector DB, API key required | `:6333` on the Compose network only | `qdrant-storage`, `qdrant-snapshots` |
| `core` | `ghcr.io/sourceblender/musubi-core` (digest-pinned) | HTTP API (uvicorn) | `127.0.0.1:8100` on the host by default | `vault`, `artifact-blobs`, `lifecycle`, `logs` |
| `lifecycle-worker` | same image as `core` | Background jobs (`python -m musubi.lifecycle.runner`) | `:8101/metrics` on the Compose network only | `vault`, `artifact-blobs`, `lifecycle`, `logs` |

Optional local-GPU overlay (`-f deploy/docker/compose.local-gpu.yml`):

| Service | Image | Role | Exposes | Volumes |
|---|---|---|---|---|
| `tei-dense` | operator-chosen TEI image (`MUSUBI_TEI_IMAGE`, digest-pinned) | BGE-M3 dense embeddings | `:80` on the Compose network only | `tei-models` |
| `tei-sparse` | same TEI image | SPLADE v3 sparse embeddings | `:80` on the Compose network only | `tei-models` |
| `tei-reranker` | same TEI image | `bge-reranker-v2-m3` reranking | `:80` on the Compose network only | `tei-models` |
| `ollama` | operator-chosen Ollama image (`MUSUBI_OLLAMA_IMAGE`, digest-pinned) | LLM for lifecycle jobs | `:11434` on the Compose network only | `ollama-models` |

Only `core` publishes a host port. Without the overlay, the four inference URLs in `.env` point at endpoints the operator runs elsewhere. TLS termination is an operator-provided reverse proxy in front of `127.0.0.1:8100`; it is not part of the stack.

## Resource envelopes

Measured figures for the reference host (Ryzen 5, 32 GB RAM, RTX 3080 10 GB) are in [[09-operations/capacity]] and the VRAM budget is in [[08-deployment/gpu-inference-topology]].

## Startup order

Compose `depends_on` conditions enforce this order:

1. `volume-init` runs to completion, and `qdrant` passes its TCP health check.
2. With the GPU overlay, `tei-dense`, `tei-sparse`, `tei-reranker` and `ollama` must also be healthy before `core` starts.
3. `core` starts and must pass `GET /v1/ops/health`.
4. `lifecycle-worker` starts after `core` is healthy.

## Restart policies

Every long-running service uses `restart: unless-stopped`. `volume-init` uses `restart: "no"`.

## Health checks

- `core`: `curl -fsS http://127.0.0.1:8100/v1/ops/health` (liveness). `GET /v1/ops/status` reports per-component readiness for Qdrant, the three TEI services and Ollama.
- `lifecycle-worker`: reads `:8101/metrics` and requires `musubi_lifecycle_coordinator_ready` to equal 1.
- `qdrant` and the TEI services: TCP connect to their port. `ollama`: `ollama list`.

There are no `/healthz` or `/readyz` endpoints.

## Crash blast radius

| Crash | Impact |
|---|---|
| `core` | Read/write API unavailable. The lifecycle worker keeps running its jobs. |
| `lifecycle-worker` | No maturation, synthesis, promotion, demotion or vault reconcile. No direct user-facing impact; jobs resume on restart. |
| `qdrant` | Reads and writes fail with error responses. |
| dense TEI | Capture and retrieval cannot embed and fail. |
| sparse TEI | Retrieval falls back to dense-only and returns a `sparse_embedding_failed` warning. |
| reranker TEI | Retrieval returns unreranked results with a `reranker_failed` warning. |
| Ollama / lifecycle LLM | Importance scoring, synthesis, promotion and reflection stall. Non-critical for the API. |
| reverse proxy | External reachability broken. Loopback access and internal containers continue. |

## Logging and observability

- Services log structured JSON to stdout/stderr, captured by Docker's default logging driver. `LOG_DIR` (`/var/log/musubi`, `logs` volume) is mounted, but nothing writes to it today.
- Core serves Prometheus metrics at `GET /v1/ops/metrics`; the worker at `:8101/metrics`.
- Core reads `X-Request-Id` from the request (or mints one), echoes it on the response and carries it in log lines as `request_id`.
- See [[09-operations/observability]].

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the component specs in sections 04–10, each of which carries its own `## Test Contract` section.
