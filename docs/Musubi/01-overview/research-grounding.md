---
title: Research Grounding
section: 01-overview
tags: [overview, references, research, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-10-01
up: "[[01-overview/index]]"
reviewed: false
---
# Research Grounding

Musubi's design is informed by the state of the art in AI agent memory systems as of 2026. This document summarizes which ideas we adopt, which we modify, and which we reject, with citations.

## Adopted — in roughly this form

### Stanford Generative Agents (Park et al., 2023): retrieval scoring triad
- **What we take:** Retrieval score = weighted combination of recency + importance + relevance, with importance as a 1–10 LLM-assigned score at ingestion.
- **What we modify:** Added weights for provenance (plane and lifecycle state) and reinforcement (`src/musubi/retrieve/scoring.py`).
- **Where in the design:** [[05-retrieval/scoring-model]].

### Letta / MemGPT (Packer et al., 2023; Letta docs 2026): hierarchical memory tiers + agent self-editing
- **What we take:** The idea that agents manage their own memory via explicit tool calls. Our MCP tools `musubi_search`, `musubi_get`, `musubi_remember`, `musubi_recent` and `musubi_think` (`src/musubi/adapters/mcp/tools.py`) descend from this.
- **What we modify:** We do not implement core/recall/archival as a single-agent context-window hierarchy. Musubi is cross-agent shared memory, not per-agent context management. Episodic/Curated/Artifact is our equivalent hierarchy, but the planes have different semantics, not different proximity to the agent's context.
- **Where in the design:** [[07-interfaces/canonical-api]] (tool ergonomics), [[04-data-model/lifecycle]] (state transitions).

### Mem0 (arxiv 2504.19413): fact extraction + ADD/UPDATE/DELETE/NOOP consolidation
- **What we take:** The extract-and-consolidate pattern. Our [[06-ingestion/concept-synthesis|Synthesis job]] runs an analogous pipeline: entity/fact extraction, comparison to existing concepts, one of {create, reinforce, merge, ignore}.
- **What we modify:** We don't conflate the vector store with the graph store. Synthesized concepts live in Qdrant with lineage fields; curated relationships live as wikilinks in the vault.
- **Where in the design:** [[06-ingestion/concept-synthesis]], [[06-ingestion/maturation]].

### Zep / Graphiti (arxiv 2501.13956): bitemporal validity windows
- **What we take:** Facts have two time axes — when they were true in the world (event time) and when the system learned about them (ingestion time). Curated knowledge and synthesized concepts both record both.
- **What we modify:** We do not build a full temporal knowledge graph. Bitemporal fields (`event_at`, `ingested_at` on episodic memories) live on memory objects, and supersession/validity windows are tracked via `supersedes` / `superseded_by` + `valid_from` / `valid_until`.
- **Where in the design:** [[04-data-model/temporal-model]].

### Qdrant hybrid search (sparse + dense + server-side fusion)
- **What we take:** Named dense and sparse vectors per collection and the server-side Query API with RRF fusion. Musubi stores BGE-M3 dense vectors and SPLADE v3 sparse vectors and runs Qdrant 1.17.
- **Where in the design:** [[05-retrieval/hybrid-search]], [[04-data-model/qdrant-layout]].

### LiveKit Agents dual-agent RAG pattern (VoiceAgentRAG, arxiv 2603.02206)
- **What we take:** The "Slow Thinker pre-fetches, Fast Talker reads from cache" pattern for staying inside the 200ms conversational budget. Our fast-path cache is populated speculatively on partial transcripts.
- **What we modify:** Because embedding and retrieval are self-hosted, on-demand retrieval fits the budget in most cases. Pre-fetch is a latency insurance policy, not the primary path. The pre-fetch logic lives in the LiveKit integration (`sourceblender/musubi-livekit`).
- **Where in the design:** [[05-retrieval/fast-path]], [[07-interfaces/livekit-adapter]].

### MCP Authorization (spec finalized June 2025)
- **What we take:** OAuth 2.1 with dynamic client registration is the right shape *if* we go multi-org. For a small team, Musubi uses JWT bearer tokens: HS256 with a shared signing key, or RS256 verified against a JWKS endpoint (`src/musubi/auth/tokens.py`). There is no mTLS.
- **Where in the design:** [[10-security/auth]].

## Referenced but modified heavily

### Obsidian as a store of record (community pattern, multiple python tools)
- Python ecosystem: `obsidiantools`, `obsidian-metadata`, `python-frontmatter`, `watchdog`.
- We build our own minimal vault package, `src/musubi/vault` (`VaultWriter`, the watcher module and the reconciler), on `watchdog` and `ruamel.yaml` rather than taking a heavy dependency on any single tool. See [[06-ingestion/vault-sync]].

## Considered and rejected (for v1)

### Graphiti as primary store
- Rejected because small-team knowledge-graph density is low and the engineering overhead is not repaid at this scale.
- Documented in [[13-decisions/0004-no-knowledge-graph-v1]] with an explicit "when to revisit" trigger.

### MemGPT's core/recall/archival hierarchy as the primary organizing structure
- Rejected because Musubi is shared across agents; per-agent working memory is the adapter's job.
- See [[13-decisions/0002-planes-not-tiers]].

### Hosted cloud embedding APIs as the primary path
- Rejected for the hot path. Latency, lock-in and cost all favor self-hosted models.
- No secondary hosted-embedding vector is implemented; the named-vector scheme leaves room for one.
- See [[13-decisions/0006-pluggable-embeddings]].

### SQLite or Postgres for metadata
- Rejected for v1. All memory metadata lives in Qdrant payloads (which are indexable) or in the vault frontmatter. The lifecycle worker keeps its own small SQLite work store (`LIFECYCLE_SQLITE_PATH`) for lifecycle coordination state. Introducing a third store adds backup complexity without clear wins at this scale.
- Noted as a post-v1 possibility if structured relational queries become a bottleneck.
- See [[13-decisions/0008-no-relational-store]].

## Reference reading for contributors

Specs have a "Prior Art" section linking to the external system idea being borrowed from. Someone new to an area should read:

1. Their spec.
2. The ADRs it references.
3. The Prior Art papers / docs listed in the spec.

This is faster than reading this vault cover to cover.

## Citation style

External references in specs are rendered as standard markdown links (not wikilinks) and collected in [[13-decisions/sources]] for the bibliography.
