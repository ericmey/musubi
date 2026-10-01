---
title: Components
section: 03-system-design
tags: [architecture, components, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Components

Every component in Musubi. Each has a clear responsibility, inputs, outputs, and ownership boundary.

> The stack is defined in the root `docker-compose.yml`, with an optional GPU overlay in `deploy/docker/compose.local-gpu.yml`. See [[08-deployment/compose-stack]].

## Musubi Core (`src/musubi/`, `core` service)

**What it owns:** the canonical HTTP API, plane-level business logic, all mutations to Qdrant, authorization decisions, request validation.

**Process:** FastAPI under uvicorn (`musubi.api.app:create_app`), listening on `:8100` inside the container. Compose publishes it on `127.0.0.1:8100` by default (`MUSUBI_CORE_BIND`, `MUSUBI_CORE_PORT`).

**Depends on:**
- Qdrant (`qdrant:6333`, API key required) for every plane's index.
- Three TEI endpoints: dense (`TEI_DENSE_URL`), sparse (`TEI_SPARSE_URL`) and reranker (`TEI_RERANKER_URL`). Core probes Qdrant and the dense endpoint at startup.
- Ollama (`OLLAMA_URL`). Core does not call the LLM on the request path; only the operator-only `POST /v1/ops/debug/trigger-synthesis` hook does.
- Named volumes: `vault` → `/var/lib/musubi/vault`, `artifact-blobs` → `/var/lib/musubi/artifact-blobs`, `lifecycle` → `/var/lib/musubi/lifecycle`, `logs` → `/var/log/musubi`.

**Does not own:**
- Background jobs (lifecycle worker).
- Model serving (TEI and Ollama, local or remote).
- Adapter protocols, except the in-repo MCP server and CLI, which are clients of the API.

**Key modules:**
- `src/musubi/api/` — HTTP routers (`routers/`), auth dependencies, idempotency, rate limits. Thin delegation.
- `src/musubi/planes/episodic/`, `curated/`, `artifact/`, `concept/`, `thoughts/` — plane business logic.
- `src/musubi/retrieve/` — hybrid search, scoring, fast and deep paths, reranking, blended and orchestrated retrieval, context packs.
- `src/musubi/auth/` — JWT validation, scope checks.
- `src/musubi/embedding/` — TEI clients and the embedding cache; `src/musubi/llm/` — Ollama and OpenAI-compatible LLM clients for the lifecycle jobs.
- `src/musubi/vault/` — frontmatter, `VaultWriter`, write log, reconciler, watcher module.
- `src/musubi/store/` — Qdrant collection names, specs and indexes.
- `src/musubi/types/` — pydantic v2 schemas shared across modules.
- `src/musubi/settings.py` (the `Settings` model) and `src/musubi/config.py` (the `get_settings()` accessor, the only place that reads the environment).

## Lifecycle worker (`src/musubi/lifecycle/`, `lifecycle-worker` service)

**What it owns:** running background jobs. Same image and codebase as Core, different entrypoint: `python -m musubi.lifecycle.runner`.

**Process:** one long-running asyncio process with a minute-resolution tick runner, no APScheduler ([[13-decisions/0025-lifecycle-runner-without-apscheduler|ADR 0025]]). No HTTP API; it serves Prometheus metrics on `:8101/metrics` (`LIFECYCLE_METRICS_PORT`), and its Compose health check requires `musubi_lifecycle_coordinator_ready 1` there. It starts after Core is healthy.

**Jobs** (registered in `src/musubi/lifecycle/runner.py`; times are UTC):

| Job | Cadence | Purpose |
|---|---|---|
| `maturation_episodic` | Hourly at :13 | Move provisional episodic memories to matured; score importance, infer topics. |
| `provisional_ttl` | Hourly at :17 | Archive provisional memories older than 7 days. |
| `synthesis` | Daily 03:00 | Cluster matured memories into synthesized concepts; check contradictions. |
| `concept_maturation` | Daily 03:30 | Move synthesized concepts to matured. |
| `promotion` | Daily 04:00 | Promote eligible concepts; write curated files to the vault. |
| `demotion_concept` | Daily 05:00 | Demote matured concepts not reinforced for 30 days. |
| `demotion_episodic` | Sundays 03:45 | Demote matured episodic memories older than 60 days with importance < 4 and no access or reinforcement. |
| `demotion_artifact` | Sundays 04:15 | Archive unreferenced artifacts older than 180 days; a no-op unless `MUSUBI_ARTIFACT_ARCHIVAL_ENABLED=true`. |
| `reflection_digest` | Daily 06:00 | Write a reflection note to the vault and emit a thought. |
| `vault_reconcile` | Every 6 hours | Re-sync vault files into the curated index. |
| `lifecycle_reconcile` | Every `LIFECYCLE_RECONCILE_INTERVAL_S` (default 5 s) | Apply pending lifecycle transitions from the work store. |

**Why separate from Core:** synthesis and promotion call the LLM and can run for minutes. Running them inside the API process would starve request handling, and a worker crash must not affect request availability.

**Depends on:** same as Core, plus a SQLite work store at `LIFECYCLE_SQLITE_PATH` (`/var/lib/musubi/lifecycle/work.sqlite`), with job locks and the vault write log beside it.

## Vault watcher (module only)

`src/musubi/vault/watcher.py` implements a `watchdog`-based watcher with a 2-second per-file debounce and a write-log check that skips Musubi's own writes. **No service runs it** in the shipped stack. Human edits reach the curated index through the lifecycle worker's `vault_reconcile` job every 6 hours. Running the watcher as a real-time process is future work.

## Inference services

Core and the worker reach inference only through the four URLs in `.env`. They can point at:

- **Remote endpoints** the operator runs (the default; the root Compose file starts no inference service), or
- **The local GPU overlay** (`deploy/docker/compose.local-gpu.yml`), which adds four GPU services with no host ports, models cached in the `tei-models` and `ollama-models` volumes:

| Service | Serves | Default model |
|---|---|---|
| `tei-dense` | dense embeddings | `BAAI/bge-m3` (1024-d) |
| `tei-sparse` | sparse embeddings (`--pooling splade`) | `naver/splade-v3` |
| `tei-reranker` | reranking | `BAAI/bge-reranker-v2-m3` |
| `ollama` | LLM for lifecycle jobs | `LLM_MODEL` (`qwen3:4b` in `.env.example`) |

The operator picks the TEI image for their GPU (`MUSUBI_TEI_IMAGE` + digest) and the Ollama image (`MUSUBI_OLLAMA_IMAGE` + digest). The lifecycle LLM can instead use any OpenAI-compatible endpoint ([[13-decisions/0043-lifecycle-llm-openai-compatible-endpoint|ADR 0043]]). See [[08-deployment/gpu-inference-topology]] for the VRAM budget.

## Qdrant (`qdrant` service)

- Single node, `qdrant/qdrant:v1.17.1` pinned by digest, API key required, no host port.
- Named volumes `qdrant-storage` and `qdrant-snapshots`.
- One collection per plane (`musubi_episodic`, `musubi_curated`, `musubi_concept`, `musubi_artifact`, `musubi_artifact_chunks`, `musubi_thought`, `musubi_lifecycle_events`); namespaces are payload filters (see [[03-system-design/namespaces]]).
- Named vectors on the searchable collections (`dense_bge_m3_v1` + `sparse_splade_v1`).

## Artifact blobs

Plain files under `ARTIFACT_BLOB_PATH` (`artifact-blobs` volume), stored at `<namespace>/<object_id>` with the SHA-256 recorded in the artifact metadata.

## `volume-init` (one-shot)

Runs once before Core starts and `chown`s the four Musubi volume roots to the image's non-root user (UID 999, GID 985), because new named volumes start root-owned.

## Clients

See [[07-interfaces/index]].

| Client | Where | Talks to Core via |
|---|---|---|
| MCP server | `src/musubi/adapters/mcp` (stdio or SSE) | HTTP + SDK |
| `musubi` CLI | `src/musubi/cli` (`context`, `promote force`, `promote reject`, `validate`) | HTTP |
| Python SDK | `src/musubi/sdk`, also `sourceblender/musubi-sdk` | HTTP |
| LiveKit | `sourceblender/musubi-livekit` (core keeps a compatibility shim) | HTTP + SDK |
| OpenClaw and other agent integrations | separate `sourceblender/musubi-*` repos | HTTP |

Adapters and the SDK import only `musubi.sdk` and `musubi.types`, never plane or API modules (AGENTS.md import discipline).

## External dependencies

- **Host:** Linux with Docker Engine and the Compose v2 plugin; an NVIDIA GPU and the NVIDIA Container Toolkit only for the local GPU overlay. See [[08-deployment/host-profile]].
- **TLS:** an operator-provided TLS reverse proxy in front of Core's loopback port. Core itself serves plaintext (`MUSUBI_ALLOW_PLAINTEXT=true` in Compose).
- No other external dependencies. Specifically: no Kafka, no Redis, no Postgres.

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the component specs in sections 04–10, each of which carries its own `## Test Contract` section.
