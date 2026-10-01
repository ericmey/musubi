---
title: Root Index
section: 00-index
tags: [index, navigation, section/index, status/complete, type/index]
type: index
status: complete
updated: 2026-10-01
reviewed: false
---
# Root Index

Musubi is the **shared memory and knowledge plane** for a small team of AI agents. It is a standalone server. Every interface calls Musubi over the canonical HTTP API: the MCP server, the CLI and the Python SDK ship in this repo; LiveKit, OpenClaw and the other agent integrations are separate repositories.

## Mental model in one picture

```
                       Humans                 Agents (Claude, LiveKit, etc.)
                          │                         │
                     edits Obsidian            calls adapter
                       vault files                 │
                          │                         ▼
                          │            ┌──────────────────────────┐
                          │            │  Clients                 │
                          │            │  MCP, CLI, SDK, LiveKit, │
                          │            │  OpenClaw, plain HTTP    │
                          │            └────────────┬─────────────┘
                          │                         │  canonical API
                          ▼                         ▼
                 ┌─────────────────────────────────────────────────┐
                 │                 Musubi Core Server              │
                 │  ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │
                 │  │  Episodic   │  │  Curated    │  │ Source  │  │
                 │  │  Plane      │  │  Knowledge  │  │ Artifact│  │
                 │  │ (Qdrant)    │  │  Plane      │  │ Plane   │  │
                 │  │             │  │  (Obsidian  │  │ (blob   │  │
                 │  │             │  │   vault +   │  │ dir +   │  │
                 │  │             │  │   Qdrant    │  │ Qdrant  │  │
                 │  │             │  │   index)    │  │ chunks) │  │
                 │  └─────────────┘  └─────────────┘  └─────────┘  │
                 └─────────────────────────────────────────────────┘
                                          ▲ same Qdrant, vault, blobs
                 ┌─────────────────────────────────────────────────┐
                 │  Lifecycle worker (separate process):           │
                 │  maturation, synthesis, promotion, demotion,    │
                 │  reflection, vault reconcile                    │
                 └─────────────────────────────────────────────────┘
```

## The three planes

Musubi separates memory into three planes with different truth models. This separation is load-bearing — it is why the system can be both **fast** (episodic) and **accurate** (grounded in artifacts) and **durable** (curated).

- **[[04-data-model/episodic-memory|Episodic Plane]]** — source-first, modality-agnostic, optimized for latency. Where "who said what, when" lives.
- **[[04-data-model/curated-knowledge|Curated Knowledge Plane]]** — topic-first, human-authoritative. The Obsidian vault is the store of record; Qdrant is a derived index rebuildable from the vault.
- **[[04-data-model/source-artifact|Source Artifact Plane]]** — raw transcripts, documents, logs. Ground truth for RAG and chain of custody.

Plus a bridge layer:

- **[[04-data-model/synthesized-concept|Synthesized Concept Memory]]** — higher-order memory objects that emerge from repeated reinforcement in the episodic plane and may be promoted into curated knowledge.

## Sections

| # | Section | Purpose |
|---|---|---|
| 00 | [[00-index/index|Index]] | You are here. Navigation, executive summary, guardrails. |
| 01 | [[01-overview/index|Overview]] | Mission, scope, stakeholders, the three planes explained. |
| 03 | [[03-system-design/index|System design]] | Component architecture. Core abstraction boundary. Namespaces. |
| 04 | [[04-data-model/index|Data model]] | Schemas, relationships, lifecycle states. |
| 05 | [[05-retrieval/index|Retrieval]] | Scoring formula, fast/deep/blended paths, orchestration queries. |
| 06 | [[06-ingestion/index|Ingestion]] | Capture, maturation, synthesis, promotion, demotion. Obsidian sync. |
| 07 | [[07-interfaces/index|Interfaces]] | Canonical HTTP API, SDK, adapter specs (MCP, LiveKit, OpenClaw). |
| 08 | [[08-deployment/index|Deployment]] | Docker Compose install, host requirements, secrets, GPU topology. |
| 09 | [[09-operations/index|Operations]] | Backup, observability, runbooks, canonical vs derived assets. |
| 10 | [[10-security/index|Security]] | Auth, tenant isolation, PII handling, redaction. |
| 11 | Migration | Schema and re-embedding migrations. See [[11-migration/re-embedding]]. |
| 12 | [[12-roadmap/index|Roadmap]] | Longer-range direction (v2, v3). Open work is tracked as GitHub issues. |
| 13 | [[13-decisions/index|Decisions]] | ADRs for every load-bearing choice. |

## Key landing pages

- [[00-index/reading-tour|Reading Tour]] — plain-English guided path for first-time review.
- [[00-index/architecture.canvas|Architecture Canvas]] — visual map of the whole system.
- [[_tools/README|Vault Tools]] — `check.py`, the docs health check CI runs.
- [[00-index/executive-summary|Executive Summary]] — top 5 decisions and top 5 risks.
- [[CLAUDE|Coding agent entry point]] — CLAUDE.md. Start here if you're an agent, not a human.
- [[00-index/agent-guardrails|Agent Guardrails]] — Qdrant and vault rules; the contributor contract is AGENTS.md at the repo root.
- [[00-index/definition-of-done|Definition of Done]] — the universal merge checklist.
- [[00-index/research-questions|Research Questions]] — consolidated open questions across the vault.
- [[00-index/test-index|Test Contract Index]] — every spec's behaviour-under-test.
- [[00-index/glossary|Glossary]] — vocabulary used across the vault.
- [[00-index/conventions|Conventions]] — naming, frontmatter, link style.

## External research grounding

This design draws on:

- **MemGPT / Letta** three-tier OS-inspired memory (core / recall / archival) with agent self-editing.
- **Mem0** fact-extraction pipeline with ADD/UPDATE/DELETE/NOOP operations and graph-enhanced variant.
- **Zep / Graphiti** temporal knowledge graph with bitemporal modeling (event time + ingestion time) and fact validity windows.
- **Stanford Generative Agents** retrieval scoring (relevance + recency + importance) with LLM-rated importance.
- **Qdrant** named dense and sparse vectors with Query API server-side fusion (Musubi runs Qdrant 1.17).
- **BGE-M3** dense embeddings and **SPLADE v3** learned sparse embeddings, served by Text Embeddings Inference.
- **LiveKit Agents** dual-agent RAG pattern (Slow Thinker pre-fetches; Fast Talker reads from cache) and `on_user_turn_completed` hook.
- **MCP Authorization spec** (finalized June 2025) — OAuth 2.1 with dynamic client registration.

Full citations in [[13-decisions/sources|Decision Sources]].
