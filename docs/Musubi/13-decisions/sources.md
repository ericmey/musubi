---
title: Sources
section: 13-decisions
tags: [bibliography, decisions, section/decisions, sources, status/complete, type/adr]
type: adr
status: complete
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# Sources

Public prior art and reference material that informed the ADRs. This is not an exhaustive literature review — just the sources that actually changed a decision or sharpened a framing.

## Memory architectures

- **Letta (formerly MemGPT)** — `letta.com`, "MemGPT: Towards LLMs as Operating Systems" (Packer et al., 2023). Three-tier memory (core / archival / recall) modeled on OS paging. Informed [[13-decisions/0001-three-plane-architecture]] and the vocabulary of "episodic" vs "permanent" memory. We depart by making planes orthogonal, not hierarchical ([[13-decisions/0002-planes-not-tiers]]).
- **Mem0** — `mem0.ai`, "Mem0: Building Production-Ready AI with Scalable Long-Term Memory" (2024). Explicit extract → update → summarize phases. Informed our lifecycle pipeline, especially synthesis and maturation.
- **Zep** — `getzep.com`, "Zep: A Temporal Knowledge Graph Architecture for Agent Memory" (2024). Bitemporal relational graph. Read carefully; we chose not to build a graph in v1 ([[13-decisions/0004-no-knowledge-graph-v1]]) but the bitemporal idea (fact valid-from / valid-to) influences how we think about contradictions.
- **Stanford Generative Agents** — Park et al., "Generative Agents: Interactive Simulacra of Human Behavior" (2023). Memory streams + reflection-generated meta-memories. Direct inspiration for [[06-ingestion/reflection]] and the concept plane.
- **A-MEM** — "A-MEM: Agentic Memory for LLM Agents" (2024). Structured memory with hierarchical indexing. Mostly overlaps with Letta / Mem0; confirmed our direction.

## Retrieval

- **BEIR benchmark** — Thakur et al., "BEIR: A Heterogeneous Benchmark for Zero-shot Evaluation of Information Retrieval Models" (2021). Source of cross-domain retrieval evaluation methodology. Informs our eval suite.
- **MTEB leaderboard** — `huggingface.co/spaces/mteb/leaderboard`. Tracked through April 2026. Source of confidence that BGE-M3 + SPLADE++ V3 + cross-encoder rerank is a strong pipeline.
- **"Reciprocal Rank Fusion outperforms Condorcet and Individual Rank Learning Methods"** — Cormack et al., 2009. The theoretical basis for RRF; still a standard.
- **ColBERT / ColBERT-v2** — Khattab et al., 2020/2022. Considered and not used in v1 (cost). Noted for future.
- **Qdrant docs on hybrid search** — `qdrant.tech/documentation/tutorials/hybrid-search/`. The `Prefetch` + `FusionQuery(fusion=Fusion.RRF)` pattern.
- **BGE-M3 paper** — "BGE M3-Embedding: Multi-Lingual, Multi-Functionality, Multi-Granularity Text Embeddings Through Self-Knowledge Distillation" (2024).
- **SPLADE++ / SPLADE v3** — Formal et al., "SPLADE v3" (2024). Sparse-first, learned sparse retrieval.

## System design & patterns

- **"Designing Data-Intensive Applications"** — Martin Kleppmann, 2017. General toolkit: durability, derived data, replication, idempotence.
- **"Release It!"** — Michael Nygard. Bulkheads, circuit breakers, runbooks. Informs [[09-operations/runbooks]].
- **Hexagonal / Ports-and-Adapters architecture** — Alistair Cockburn, 2005. The ADR 0011 adapter-repo split is in this tradition.
- **12-Factor App** — `12factor.net`. Config via env, stateless processes. Largely followed.
- **Event sourcing** — Greg Young, circa 2010. Partial adoption: lifecycle events as an append log alongside current state in Qdrant ([[13-decisions/0007-no-silent-mutation]]).

## MCP, adapters, agent tooling

- **Model Context Protocol specification** — `modelcontextprotocol.io`, spec versions through June 2025 (OAuth 2.1 transport). Source for MCP adapter design.
- **LiveKit Agents** — `docs.livekit.io/agents/`. Real-time voice agent framework; informs [[07-interfaces/livekit-adapter]] dual-agent pattern.
- **OAuth 2.1 draft** — IETF `draft-ietf-oauth-v2-1`. Used for MCP auth. PKCE for public clients.

## Infrastructure

- **Qdrant documentation** — `qdrant.tech/documentation/`. Especially named vectors, hybrid search, quantization, snapshots. v1.15 is the baseline.
- **Hugging Face Text Embeddings Inference (TEI)** — `github.com/huggingface/text-embeddings-inference`. Used for both dense and rerank; sparse added via SPLADE-serving adaptation.
- **Ollama** — `ollama.com`. Used for local LLM; well-maintained, simple API.
- **APScheduler** — `apscheduler.readthedocs.io`. Lifecycle engine.
- **Ansible** — `docs.ansible.com`. Config management for the host.
- **Caddy** — `caddyserver.com`. Reverse proxy + automatic TLS.

## Privacy and security

- **OWASP ASVS** — Application Security Verification Standard. Reference for auth design.
- **Prompt injection catalog** — Greshake et al., "Not what you've signed up for: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection" (2023). Informs [[10-security/prompt-hygiene]].
- **LUKS** — `gitlab.com/cryptsetup/cryptsetup`. Full-disk encryption for at-rest data.

## Reflection: what we considered and didn't cite

- **Mem3, Cognitive Architectures for Language Agents (CoALA)** — read for general framing; didn't cite specifically because our design predates and diverges.
- **GraphRAG (Microsoft)** — considered; not pursued in v1 ([[13-decisions/0004-no-knowledge-graph-v1]]).
- **LangChain memory modules** — looked at, not adopted. Their memory abstractions didn't fit our explicit-plane design.

## Updating this page

When an ADR cites a new public source, add it here. When we retire a source (model replaced, paper superseded), keep the entry but annotate it. This is a record of what shaped our thinking, not a current-best-practices list.
