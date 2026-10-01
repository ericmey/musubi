---
title: "03 — System Design"
section: 03-system-design
tags: [architecture, components, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# 03 — System Design

The component-level architecture of Musubi.

## Documents in this section

- [[03-system-design/components]] — Every process, service, and library and what it owns.
- [[03-system-design/abstraction-boundary]] — The core abstraction boundary between Musubi and everything else.
- [[03-system-design/namespaces]] — How tenants, presences, and planes partition the data.
- [[03-system-design/process-topology]] — Which things run in which processes and why.
- [[03-system-design/data-flow]] — Sequence diagrams for the main operations.
- [[03-system-design/failure-modes]] — What breaks and how we degrade.

## One-paragraph summary

Musubi Core is a Python (FastAPI) service that wraps Qdrant, the Obsidian vault and a directory of artifact blobs, and exposes a single canonical HTTP API. A **lifecycle worker** process runs maturation, synthesis, promotion, demotion, reflection and vault reconciliation against the same data on a tick runner ([[13-decisions/0025-lifecycle-runner-without-apscheduler|ADR 0025]]; 5 s tick by default, with cron-style jobs matched to their named minute); its `vault_reconcile` job picks up human edits to the vault every 6 hours. Inference is three TEI services (dense, sparse, reranker) and an Ollama LLM, either started locally by the optional GPU overlay or run elsewhere by the operator. The MCP server, CLI and SDK in this repo, and the separate integration repos, all call the canonical API. Everything runs under Docker Compose on an operator-prepared host.

> The stack is defined in the root `docker-compose.yml`, with an optional GPU overlay in `deploy/docker/compose.local-gpu.yml`. See [[08-deployment/compose-stack]].

## Component map

```
                          ┌─────────────────────────────┐
                          │  Obsidian vault             │
                          │  `vault` volume             │
                          │  /var/lib/musubi/vault      │
                          └─────────────────────────────┘
                                 ▲ promotion writes   │ vault_reconcile
                                 │                    ▼ (every 6 h)
 Clients                  ┌─────────────────────────────┐     ┌─────────────────────┐
 ───────                  │  lifecycle-worker           │     │  qdrant 1.17        │
 MCP server  ──┐          │  tick runner, :8101/metrics │◄───►│  dense + sparse     │
 CLI / SDK   ──┤          └─────────────────────────────┘     │  named vectors      │
 LiveKit     ──┤                                              │  (no host port)     │
 OpenClaw    ──┤   TLS                                        │                     │
 plain HTTP  ──┴──► proxy  ┌─────────────────────────┐        │                     │
     (operator) ─────────► │  core (FastAPI)         │◄──────►│                     │
                           │  127.0.0.1:8100         │        └─────────────────────┘
                           └────────────┬────────────┘
                                        │ uses (core and worker)
                                        ▼
                          ┌─────────────────────────────────┐
                          │  Inference (local overlay or    │
                          │  remote endpoints)              │
                          │  tei-dense    BGE-M3            │
                          │  tei-sparse   SPLADE v3         │
                          │  tei-reranker bge-reranker-v2-m3│
                          │  ollama       LLM_MODEL         │
                          └─────────────────────────────────┘

                          ┌─────────────────────────────────┐
                          │  Artifact blobs                 │
                          │  `artifact-blobs` volume        │
                          │  /var/lib/musubi/artifact-blobs │
                          └─────────────────────────────────┘
```

`core` and `lifecycle-worker` share the `vault`, `artifact-blobs`, `lifecycle` and `logs` named volumes, and reach Qdrant and the inference services over the Compose network.

## Why these boundaries

See [[03-system-design/abstraction-boundary]] for the load-bearing rationale on each line. Short version:

1. **Core vs lifecycle worker** — write-path latency is different. Core responds synchronously to user requests (ms). The worker runs minutes-long jobs. Splitting them keeps a worker crash from taking down the API.
2. **Core vs inference** — inference is GPU-bound and has different scaling and restart characteristics. TEI and Ollama are proven model servers; re-implementing them inside Core would be strictly worse. Keeping them behind URLs also lets the operator run them on another host.
3. **Core vs adapters** — adapter code is protocol-specific (MCP, LiveKit, OpenClaw). Keeping it out of Core's business logic keeps Core stable.
4. **Real-time vault watching (future)** — a watcher module exists (`src/musubi/vault/watcher.py`) and would run as its own process so filesystem event bursts cannot disrupt the API. It is not deployed today; periodic reconciliation covers the same ground with up to 6 hours of lag.
