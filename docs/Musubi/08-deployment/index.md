---
title: Deployment
section: 08-deployment
tags: [deployment, index, section/deployment, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Deployment

How Musubi runs on the dedicated Ubuntu box. Single-host for v1 — no k8s, no cloud dependency.

## Target

One dedicated Linux server:

- **CPU:** AMD Ryzen 5 (6 cores / 12 threads)
- **RAM:** 32 GB
- **GPU:** NVIDIA RTX 3080 — 10 GB VRAM, CUDA 13
- **OS:** Ubuntu Server 24.04 LTS
- **Role:** dedicated to Musubi + its inference workers; no shared tenants

Everything in this section assumes this profile. If deployment scales to multi-host later, see [[11-migration/scaling]].

## Topology

```
┌───────────────────────────┐   ┌─────────────────────────────────────────────────┐
│  Kong (<kong-gateway> VM)  │   │ Musubi host (Ubuntu, dedicated)                  │
│  <kong-ip>                 │   │                                                  │
│                   │            │  ┌──────────────┐   ┌─────────────────────────┐ │
│  TLS, OAuth,               │──HTTP:8100▶│  │ Musubi Core  │──▶│ Qdrant 1.15 (container) │ │
│  rate-limit,               │   │  │   (FastAPI)  │   └─────────────────────────┘ │
│  access-log                │   │  │              │                                │
└───────────────────────────┘   │  │              │──▶┌─────────────────────────┐ │
        ▲                       │  │              │   │  TEI (dense embedding)  │ │
        │ HTTPS :443            │  │              │   │  BGE-M3 on GPU           │ │
   LAN clients                  │  │              │   └─────────────────────────┘ │
                                │  │              │                                │
                                │  │              │──▶┌─────────────────────────┐ │
                                │  │              │   │  TEI (sparse)            │ │
                                │  │              │   │  SPLADE++ V3 on GPU      │ │
                                │  │              │   └─────────────────────────┘ │
                                │  │              │                                │
                                │  │              │──▶┌─────────────────────────┐ │
                                │  │              │   │  TEI (reranker)          │ │
                                │  │              │   │  BGE-reranker-v2-m3      │ │
                                │  │              │   └─────────────────────────┘ │
                                │  │              │                                │
                                │  │              │──▶┌─────────────────────────┐ │
                                │  │              │   │  Ollama (LLM)            │ │
                                │  │              │   │  Qwen2.5-7B-Instruct Q4  │ │
                                │  │              │   └─────────────────────────┘ │
                                │  │              │                                │
                                │  │              │──▶┌─────────────────────────┐ │
                                │  │              │   │  Vault fs (watcher)      │ │
                                │  │              │   └─────────────────────────┘ │
                                │  │              │                                │
                                │  │              │──▶ APScheduler (lifecycle)    │
                                │  └──────────────┘                                │
                                └─────────────────────────────────────────────────┘
```

Kong (VLAN-wide gateway on `<kong-gateway>`) fronts Musubi. The Musubi host exposes only `<musubi-ip>:8100` on the LAN for Kong's upstream. All inference services stay on the Docker compose bridge. Everything containerized except the vault watcher (runs in-process with Musubi Core for filesystem-event latency). See [[13-decisions/0014-kong-over-caddy]] for the gateway decision.

> Hostnames and IPs in this section use placeholder tokens. Real values in `.agent-context.local.md` (gitignored).

## Docs in this section

- [[08-deployment/host-profile]] — Host hardware, OS provisioning, CUDA drivers, Docker, systemd, ports.
- [[08-deployment/ansible-layout]] — Ansible roles + inventory structure used to provision and keep the host in sync.
- [[08-deployment/gpu-inference-topology]] — How TEI + Ollama share the 10 GB VRAM; model loading order; fallbacks.
- [[08-deployment/qdrant-config]] — Qdrant container, storage layout, quantization, HNSW params, WAL, snapshots.
- [[08-deployment/compose-stack]] — The `docker compose` stack: services, networks, volumes, health checks.
- [[08-deployment/kong]] — Kong API gateway on `<kong-gateway>`: TLS, auth, rate limits, routing to Musubi. Replaces the former Caddy role — see [[13-decisions/0014-kong-over-caddy]].

## Principles

1. **One box per v1.** Simpler ops, lower latency, fewer moving parts. Multi-host is explicit migration work, not accidental drift.
2. **Containerize everything except the watcher.** The watcher lives with Core; everything else is pinned by digest.
3. **No cloud dependency.** The only internet-facing requirement is OAuth (could be self-hosted too). Embeddings/LLMs run local.
4. **Restart safety.** Any component can restart any time; Qdrant rehydrates, TEI reloads the model, Core resumes. No live-data migration on restart.
5. **Bring-up is one command.** `ansible-playbook musubi.yml` → working host from scratch in ~15 min assuming model weights cached.
6. **Backup is out-of-band.** See [[09-operations/backup-restore]]. Snapshots go to an attached drive; vault goes to git.

## Pinning versions

| Component | Pin |
|---|---|
| Qdrant | `qdrant/qdrant:v1.17.1` (per [[13-decisions/0023-qdrant-version-bump-to-1-17]]) |
| TEI dense | `ghcr.io/huggingface/text-embeddings-inference:1.5-cuda` |
| TEI sparse | same image, different model |
| TEI reranker | same image |
| Ollama | `ollama/ollama:0.4.0-cuda` (or newer compatible) |
| Musubi Core | `ghcr.io/ericmey/musubi-core:v1.0.0` (self-built) |

Kong runs on `<kong-gateway>` (a separate VM); its image is pinned in Kong's own deployment repo, not Musubi's. See [[08-deployment/kong]].

All pinned by **digest** in `docker-compose.yml`, not tag. Tags are for humans; digests for machines.

## Bring-up order

1. Qdrant (storage first).
2. TEI (dense, sparse, reranker) — in parallel; first boot pulls models.
3. Ollama — pulls Qwen2.5-7B-Instruct Q4 on first boot.
4. Musubi Core — starts after Qdrant passes health check; uses health endpoints of TEI and Ollama.

Compose health checks enforce this ordering with `depends_on.condition: service_healthy`. Kong, on `<kong-gateway>`, points its upstream at Musubi Core independently — it'll return 502 while the stack is booting, then flip to 200 once Core is healthy.
