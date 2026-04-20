---
title: Root Index
section: 00-index
tags: [index, navigation, section/index, status/complete, type/index]
type: index
status: complete
updated: 2026-04-17
reviewed: false
---
# Root Index

Musubi is the **shared memory and knowledge plane** for a small-team AI agent fleet. It is a standalone server. Every interface (MCP, LiveKit, OpenClaw, direct HTTP) is an independent downstream project that calls Musubi over the canonical API.

## Mental model in one picture

```
                       Humans                 Agents (Claude, LiveKit, etc.)
                          │                         │
                     edits Obsidian            calls adapter
                       vault files                 │
                          │                         ▼
                          │            ┌──────────────────────────┐
                          │            │  Adapter projects        │
                          │            │  musubi-mcp, musubi-lk,  │
                          │            │  musubi-openclaw, curl   │
                          │            └────────────┬─────────────┘
                          │                         │  canonical API
                          ▼                         ▼
                 ┌─────────────────────────────────────────────────┐
                 │                 Musubi Core Server              │
                 │  ┌─────────────┐  ┌─────────────┐  ┌─────────┐  │
                 │  │  Episodic   │  │  Curated    │  │ Source  │  │
                 │  │  Plane      │  │  Knowledge  │  │ Artifact│  │
                 │  │ (Qdrant)    │  │  Plane      │  │ Plane   │  │
                 │  │             │  │  (Obsidian  │  │ (object │  │
                 │  │             │  │   vault +   │  │ store + │  │
                 │  │             │  │   Qdrant    │  │ Qdrant  │  │
                 │  │             │  │   index)    │  │ chunks) │  │
                 │  └─────────────┘  └─────────────┘  └─────────┘  │
                 │           Lifecycle engine (maturation,          │
                 │           synthesis, promotion, demotion)        │
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
| 02 | [[02-current-state/index|Current state]] | Honest gap analysis. What the POC is vs what this doc asks for. |
| 03 | [[03-system-design/index|System design]] | Component architecture. Core abstraction boundary. Namespaces. |
| 04 | [[04-data-model/index|Data model]] | Schemas, relationships, lifecycle states. |
| 05 | [[05-retrieval/index|Retrieval]] | Scoring formula, fast/deep/blended paths, orchestration queries. |
| 06 | [[06-ingestion/index|Ingestion]] | Capture, maturation, synthesis, promotion, demotion. Obsidian sync. |
| 07 | [[07-interfaces/index|Interfaces]] | Canonical API, SDK, adapter specs (MCP, LiveKit, OpenClaw, REST/gRPC). |
| 08 | [[08-deployment/index|Deployment]] | Ansible, bootstrap order, secrets, container topology. |
| 09 | [[09-operations/index|Operations]] | Backup, observability, runbooks, canonical vs derived assets. |
| 10 | [[10-security/index|Security]] | Auth, tenant isolation, PII handling, redaction. |
| 11 | [[11-migration/index|Migration]] | Path from POC to target. Data migration plan. |
| 12 | [[12-roadmap/index|Roadmap]] | Phased implementation plan with agent-parallelizable slices. |
| 13 | [[13-decisions/index|Decisions]] | ADRs for every load-bearing choice. |

## Key landing pages

- [[00-index/dashboard|Vault Dashboard]] — live status snapshot, review progress, gap hotspots.
- [[00-index/reading-tour|Reading Tour]] — plain-English guided path for first-time review.
- [[00-index/architecture.canvas|Architecture Canvas]] — visual map of the whole system.
- [[_slices/index|Slice Registry]] — the parallelizable work units for coding agents.
- [[_slices/slice-dag.canvas|Slice DAG]] — visual dependency graph of all slices.
- [[_slices/completed-work|Completed Work]] — shipped slices + phase completion stats.
- [[_tools/README|Vault Tools]] — `check.py` (vault/slice/spec linter) and `slice_watch.py` (transition notifier).
- [[00-index/obsidian-setup|Obsidian Setup]] — verify-after-restart checklist for plugin configs.
- [[00-index/work-log|Work Log]] — dated record of infrastructure + implementation events. Where "X is now real" gets written down.
- [[_inbox/operator-notes|Operator Notes]] — scratchpad for reactions while reviewing.
- [[00-index/executive-summary|Executive Summary]] — top 5 decisions, top 5 risks, top 5 next steps.
- [[CLAUDE|Coding Agent Entry Point]] — CLAUDE.md. Start here if you're an agent, not a human.
- [[00-index/agent-guardrails|Agent Guardrails]] — rules for a fleet of coding agents working in this repo.
- [[00-index/agent-handoff|Agent Handoff Protocol]] — the lifecycle of a slice, step-by-step.
- [[00-index/definition-of-done|Definition of Done]] — the universal merge checklist.
- [[00-index/research-questions|Research Questions]] — consolidated open questions across the vault.
- [[00-index/stubs|Stubs & Placeholders]] — what's intentionally brief and why.
- [[00-index/test-index|Test Contract Index]] — every spec's behaviour-under-test.
- [[00-index/glossary|Glossary]] — vocabulary used across the vault.
- [[00-index/conventions|Conventions]] — naming, frontmatter, link style.
- [[_bases/index|Bases]] — dynamic filters over the vault (by status, by section, etc.).

## External research grounding

This design draws on (all current as of April 2026):

- **MemGPT / Letta** three-tier OS-inspired memory (core / recall / archival) with agent self-editing.
- **Mem0** fact-extraction pipeline with ADD/UPDATE/DELETE/NOOP operations and graph-enhanced variant.
- **Zep / Graphiti** temporal knowledge graph with bitemporal modeling (event time + ingestion time) and fact validity windows.
- **Stanford Generative Agents** retrieval scoring (relevance + recency + importance) with LLM-rated importance.
- **Qdrant 1.15+** native BM25, BM42, SPLADE++ hybrid search, named vectors, Query API server-side fusion.
- **Gemini Embedding-001** GA (3072-d default, Matryoshka to 1536/768, RETRIEVAL_QUERY/DOCUMENT task types).
- **LiveKit Agents** dual-agent RAG pattern (Slow Thinker pre-fetches; Fast Talker reads from cache) and `on_user_turn_completed` hook.
- **MCP Authorization spec** (finalized June 2025) — OAuth 2.1 with dynamic client registration.

Full citations in [[13-decisions/sources|Decision Sources]].
