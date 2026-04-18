---
title: System Design
section: 03-system-design
tags: [architecture, components, section/system-design, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
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

Musubi Core is a Python service that wraps Qdrant + the Obsidian vault + a local object store and exposes a single canonical API. A **Lifecycle Worker** process runs background maturation, synthesis, promotion, and demotion jobs against the same data. A **Vault Watcher** process watches the Obsidian vault filesystem for human edits and reindexes changed files. A **GPU Inference Pool** (TEI for embeddings + reranker, Ollama for LLM) runs as separate containers. Every adapter (MCP, LiveKit, OpenClaw, custom) is an independent project that calls the canonical API via the SDK. All processes are managed by Docker Compose, orchestrated by Ansible.

## Component map

```
                              ┌─────────────────────┐
                              │   Obsidian Vault    │
                              │  (filesystem)       │
                              │  /srv/musubi/vault  │
                              └─────────────────────┘
                                       ▲  ▼
                          ┌────────────┴──┴─────────────┐
                          │     Vault Watcher           │
                          │  (python, watchdog)         │
                          └─────────────────────────────┘
                                       │
                                       │ reindex events
                                       ▼
    Clients (adapters)      ┌─────────────────────┐        ┌─────────────────────┐
    ─────────────────       │                     │        │                     │
    musubi-mcp    ─────►    │   Musubi Core       │  ◄───► │   Qdrant 1.15+      │
    musubi-livekit ─────►   │   (FastAPI +        │        │   (dense + sparse   │
    musubi-openclaw ───►    │    gRPC, async)     │        │    named vectors)   │
    curl / SDK ─────►       │                     │        │                     │
                            └──────────┬──────────┘        └─────────────────────┘
                                       │
                                       │ uses
                                       ▼
                          ┌─────────────────────────────┐
                          │   GPU Inference Pool        │
                          │   ┌────────────────────┐    │
                          │   │ TEI (embeddings +  │    │
                          │   │ reranker) :8080    │    │
                          │   ├────────────────────┤    │
                          │   │ Ollama (importance│    │
                          │   │ + synthesis) :11434│    │
                          │   └────────────────────┘    │
                          └─────────────────────────────┘

                          ┌─────────────────────────────┐
                          │   Lifecycle Worker          │
                          │   (python, apscheduler)     │
                          │   ─ maturation              │
                          │   ─ synthesis               │
                          │   ─ promotion               │
                          │   ─ demotion                │
                          │   ─ reflection              │
                          └──────────┬──────────────────┘
                                     │
                                     │ same libs as Core
                                     ▼
                             (Qdrant, vault, inference)

                          ┌─────────────────────────────┐
                          │   Object Store              │
                          │   (filesystem or MinIO)     │
                          │   /srv/musubi/artifacts     │
                          └─────────────────────────────┘
```

All boxes are separate processes (containers). They share a local filesystem for `vault/` and `artifacts/`. They share a network for Qdrant / TEI / Ollama.

## Why these boundaries

See [[03-system-design/abstraction-boundary]] for the load-bearing rationale on each line. Short version:

1. **Core vs Lifecycle Worker** — write-path latency is different. Core responds synchronously to user requests (ms). Worker runs minutes-to-hours jobs. Splitting them prevents worker crashes from taking down the API.
2. **Core vs Vault Watcher** — the watcher is I/O-bound and event-driven; Core is request-driven. Separate lets the watcher restart without disrupting the API.
3. **Core vs Inference Pool** — inference is GPU-bound and has different scaling/restart characteristics. TEI and Ollama are battle-tested model servers; re-implementing them inside Core would be strictly worse.
4. **Core vs Adapters** — adapter code is protocol-specific (MCP, LiveKit, OpenClaw). Putting it in Core couples Core to every protocol. Keeping it out makes Core stable.
