---
title: Components
section: 03-system-design
tags: [architecture, components, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Components

Every component in Musubi. Each has a clear responsibility, inputs, outputs, and ownership boundary.

## Musubi Core (`musubi/` package, `musubi-core` container)

**What it owns:** the canonical API, plane-level business logic, all mutations to Qdrant, authorization decisions, request validation.

**Process:** FastAPI + gRPC (via `grpclib` or `grpc.aio`) running under uvicorn. Single async event loop. Listens on `:8100` (HTTP) and `:8101` (gRPC).

**Depends on:**
- Qdrant (`:6333` REST, `:6334` gRPC) — for all planes' indexes.
- TEI (`:8080`) — for embedding (dense + sparse) and reranking.
- Ollama (`:11434`) — for importance scoring, summarization (on demand only; not hot-path).
- Filesystem mounts: `/srv/musubi/vault`, `/srv/musubi/artifacts`.

**Does not own:**
- Background jobs (Lifecycle Worker).
- File watching (Vault Watcher).
- Model serving (Inference Pool).
- Adapter protocols.

**Key modules:**
- `musubi/api/` — HTTP + gRPC routers. Thin delegation.
- `musubi/planes/episodic/` — episodic plane business logic.
- `musubi/planes/curated/` — curated plane business logic.
- `musubi/planes/artifact/` — artifact plane business logic.
- `musubi/planes/synthesis/` — synthesized concept plane.
- `musubi/planes/thoughts/` — preserved POC thoughts subsystem.
- `musubi/retrieval/` — scoring, hybrid, fast-path, reranker, orchestration.
- `musubi/auth/` — bearer-token validation, tenant/presence resolution.
- `musubi/inference/` — adapter clients for TEI + Ollama.
- `musubi/vault/` — read-side vault library (metadata, frontmatter).
- `musubi/store/` — artifact blob store adapter.
- `musubi/types/` — pydantic v2 schemas shared across modules.
- `musubi/config.py` — environment + constants. Single source of truth.

## Lifecycle Worker (`musubi/lifecycle/`, `musubi-lifecycle` container)

**What it owns:** running background jobs. Same codebase as Core (imports from `musubi/planes/` etc.), but a different entrypoint.

**Process:** Python long-running process with APScheduler. No HTTP surface except `/healthz` and `/metrics`.

**Jobs:**

| Job | Cadence | Purpose |
|---|---|---|
| `maturation_sweep` | Hourly | Promote provisional → matured, score importance, normalize tags. |
| `dedup_sweep` | Hourly | Deep paraphrase-aware dedup pass across matured memories. |
| `synthesis_run` | Every 6 hours | Extract facts, create concepts from reinforcement clusters. |
| `promotion_run` | Daily | Evaluate promotion-eligible concepts; write curated files. |
| `demotion_run` | Weekly | Evaluate low-value / contradicted memories; mark demoted. |
| `reflection_digest` | Daily | Generate a per-presence reflection summary (markdown in vault). |
| `vault_full_reindex` | Weekly | Safety-net full reindex of the curated vault. |
| `snapshot_qdrant` | Nightly | Qdrant snapshot to local NAS + offsite S3. |

**Why separate from Core:** A synthesis job that calls the local LLM can take 2–10 minutes. Running it inside the API process would starve request handling. A worker crash (OOM, GPU hiccup) must not affect request availability.

**Depends on:** same as Core, plus:
- Scheduler state in `/srv/musubi/lifecycle-state.db` (sqlite) — so jobs are idempotent across restarts.

## Vault Watcher (`musubi/vault/watcher.py`, `musubi-vault-watcher` container)

**What it owns:** monitoring the Obsidian vault filesystem and reindexing changed files.

**Process:** Python with `watchdog`. No HTTP surface except `/healthz`.

**Reacts to:** file create, modify, delete, move in `vault/curated/`. Ignores `vault/_archive/`, `vault/_inbox/`, `vault/artifacts/` (separate flow).

**Debouncing:** 2-second debounce per file. Batch events by directory.

**Writes to:** Qdrant (`musubi_curated` collection) via Core's internal `curated_reindex_file(...)` function, called through an in-process Python import (not over HTTP — the watcher is same codebase, co-deployable).

**Why separate container:** filesystem events are I/O-driven and can spike (bulk git pull in the vault = hundreds of events). Isolating lets us rate-limit and back-pressure without touching the API process.

**Interaction with Core:** when a human promotes a concept via the API, Core writes the file to the vault. The Vault Watcher sees the write, tries to reindex, and detects "this file's `musubi-managed: true`, was just written by Core, skip because Core already indexed it" via a short-lived write-log in a shared sqlite. Prevents double-indexing.

## GPU Inference Pool

### TEI (Text Embeddings Inference, HuggingFace) — `musubi-tei` container
- Image: `ghcr.io/huggingface/text-embeddings-inference:1.7-gpu-cuda13`
- Serves **two routes** via named model endpoints:
  - `/embed` → BGE-M3 dense (1024-d).
  - `/embed_sparse` → SPLADE++ (v2).
  - `/rerank` → BGE-reranker-v2-m3.
- Loads all three models at startup; pinned in VRAM.
- Metrics at `/metrics`.

### Ollama — `musubi-ollama` container
- Image: `ollama/ollama:latest`
- Serves: `qwen2.5:7b-instruct-q4_K_M` for importance scoring, fact extraction, summarization.
- Lazy-loaded on first use; unloaded after 5min idle (controlled by Ollama's own TTL) so TEI can reclaim VRAM.
- Called only by Lifecycle Worker, never by Core on the hot path.

See [[08-deployment/gpu-inference-topology]] for the VRAM budget and load policy.

## Qdrant

- Single node. Persistent volume at `/srv/musubi/qdrant/storage`.
- Collections per plane + per tenant (see [[03-system-design/namespaces]]).
- Named vectors for every collection (`dense_bge_m3_v1` + `sparse_splade_v1`).

## Object Store

- **Today (single-host)**: plain filesystem at `/srv/musubi/artifacts/`. Content-addressed subdirs: `/sha256[:2]/sha256[2:]/`.
- **Future (when multi-host)**: MinIO with S3 API. Drop-in replacement via the `musubi/store/` abstraction.

## Adapter projects (separate repos)

See [[07-interfaces/index]].

| Repo | Runs | Talks to Core via |
|---|---|---|
| `musubi-mcp` | stdio (subprocess) OR streamable-http on `:8200` | HTTP + SDK |
| `musubi-livekit` | in LiveKit agent process | HTTP + SDK (asyncio) |
| `musubi-openclaw` | inside OpenClaw desktop | HTTP + SDK (TS) |
| `musubi-discord` | separate process | HTTP + SDK |
| `musubi-cli` | one-shot CLI | HTTP + SDK |

Adapters **never** import `musubi/` package modules. They depend only on the SDK.

## Operator tooling

- `musubi-cli` — operator ergonomics: `snapshot`, `restore`, `reindex`, `promote --dry-run`, `demote`, `reflect`.
- `musubi-studio` (post-v1) — web UI for browsing lifecycle state.

## External dependencies

- **Host**: Ubuntu Server (Noble/24.04 or Plucky/25.04), CUDA 13, Docker Engine, nvidia-container-toolkit.
- **Docker Compose** for process orchestration (v2 plugin).
- **Kong** as a reverse proxy (TLS, simple config). Listens on `:443` and proxies to Core on `:8100`.
- **Git** for vault versioning (nightly auto-commit job).
- No other external dependencies. Specifically: no Kafka, no Redis, no Postgres, no Nginx.

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the per-component slices listed in the sibling specs under this section, each of which carries its own `## Test Contract` section bound to an owning slice.
