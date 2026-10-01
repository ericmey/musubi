---
title: Deployment
section: 08-deployment
tags: [deployment, index, section/deployment, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Deployment

How Musubi runs: a Docker Compose stack on one host that the operator has already
prepared. Single-host for v1: no Kubernetes, no cloud dependency. The step-by-step
install is in the user guide (`docs/guide/install.md`); this section explains the
pieces.

## Reference host (where capacity numbers were measured)

The latency and capacity figures in these docs were measured on one host:

- **CPU:** AMD Ryzen 5 (6 cores / 12 threads)
- **RAM:** 32 GB
- **GPU:** NVIDIA RTX 3080, 10 GB VRAM
- **Disk:** NVMe SSD

It is a point of comparison, not a requirement. What the host itself must provide
is in [[08-deployment/host-profile]].

## Topology

```
 agents / clients
       │  HTTPS (optional: your own TLS reverse proxy)
       ▼
 ┌─────────────────────────── one host, Docker Compose project "musubi" ───────────────┐
 │                                                                                      │
 │  core  (published on 127.0.0.1:8100 by default; the only host port)                 │
 │    │──▶ qdrant            (Compose network only, API key required)                   │
 │    │──▶ tei-dense / tei-sparse / tei-reranker / ollama                               │
 │    │       either remote endpoints set by URL in .env,                               │
 │    │       or the GPU overlay services (Compose network only)                        │
 │                                                                                      │
 │  lifecycle-worker  (same image; scheduled jobs; metrics on :8101, network-only)      │
 │    │──▶ qdrant, TEI, LLM endpoint, and Core's shared volumes                         │
 │                                                                                      │
 │  volume-init  (one-shot: chowns fresh volumes to UID 999/GID 985, then exits)        │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

Only Core publishes a host port, `MUSUBI_CORE_BIND:MUSUBI_CORE_PORT`, defaulting to
`127.0.0.1:8100`. Qdrant, the lifecycle worker and the GPU overlay's inference services
publish none. To reach Core from other machines, put a TLS reverse proxy in front of it:
see [[08-deployment/kong]].

The public stack does not run a real-time vault watcher. The lifecycle worker's
`vault_reconcile` job walks the vault every 6 hours and picks up changes. A standalone
watcher entrypoint exists (`python -m musubi.vault.watcher`) but is not part of the
Compose stack.

## Docs in this section

- [[08-deployment/host-profile]] — What the operator's host must provide, and the reference host spec.
- [[08-deployment/compose-stack]] — The `docker compose` stack: services, volumes, startup order, required inputs.
- [[08-deployment/gpu-inference-topology]] — The optional GPU overlay: how TEI and Ollama share a 10 GB card.
- [[08-deployment/qdrant-config]] — Qdrant container, collections, quantization, HNSW, snapshots.
- [[08-deployment/kong]] — Exposing Core over a network: TLS in a reverse proxy of your choice.

## Principles

1. **One box per v1.** Simpler ops, lower latency, fewer moving parts. Multi-host is
   explicit migration work, not accidental drift.
2. **Containers only.** Core, the lifecycle worker and Qdrant all run in Compose; so do
   TEI and Ollama when you use the GPU overlay.
3. **No cloud dependency.** Embeddings and the LLM can run locally (GPU overlay) or at
   endpoints you choose.
4. **Restart safety.** Every service has `restart: unless-stopped`. Qdrant replays its
   WAL on restart; Core re-probes its dependencies at startup.
5. **Bring-up is one command.** `docker compose up -d --wait`, or with the GPU overlay
   `docker compose -f docker-compose.yml -f deploy/docker/compose.local-gpu.yml up -d --wait`.
   With the overlay, pull the LLM once afterwards (see `docs/guide/install.md`).
6. **Backup is cold and out-of-band.** See [[09-operations/backup-restore]].

## Pinning versions

| Component | Pin |
|---|---|
| Qdrant | `qdrant/qdrant:v1.17.1@sha256:…` in `docker-compose.yml` (per [[13-decisions/0023-qdrant-version-bump-to-1-17]]) |
| Musubi Core + lifecycle worker | `ghcr.io/sourceblender/musubi-core@sha256:…` on the `x-core-image` anchor (signed; see `docs/guide/install.md`) |
| TEI dense / sparse / reranker (overlay) | `MUSUBI_TEI_IMAGE` + `MUSUBI_TEI_DIGEST` in `.env`: one image the operator picks for their GPU |
| Ollama (overlay) | `MUSUBI_OLLAMA_IMAGE` + `MUSUBI_OLLAMA_DIGEST` in `.env` |

Every image in the shipped stack is pinned by digest. The overlay refuses to start
without the TEI and Ollama digests.

## Bring-up order

1. `volume-init` runs and exits; `qdrant` starts and passes its health check.
2. `core` starts after both. With the GPU overlay it also waits for all four inference
   services to be healthy. At startup Core probes Qdrant and the dense TEI endpoint and
   refuses to become ready if either stays unreachable.
3. `lifecycle-worker` starts once Core is healthy. Its health check passes when
   `musubi_lifecycle_coordinator_ready` reads `1`.

Compose enforces this with `depends_on` conditions.
