---
title: Glossary
section: 00-index
tags: [reference, section/index, status/complete, type/index, vocabulary]
type: index
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Glossary

Terms used across this vault. If a term is ambiguous in general usage, this file pins down what Musubi means by it.

## Core terms

- **Plane** — One of three top-level memory partitions: Episodic, Curated Knowledge, Source Artifact. Each has its own write path, truth model, and retention policy. See [[01-overview/three-planes]].
- **Namespace** — A scoping identifier that partitions all memory objects. Shape: `{tenant}/{presence}/{plane}` (e.g., `alex/claude-code/episodic`). Always explicit; never defaulted. See [[03-system-design/namespaces]].
- **Presence** — The channel or client a tenant (agent) speaks through, such as `voice`, `discord` or `claude-code`. A token names it as `tenant/presence` (for example `alex/voice`) in its `presence` claim, which must equal the token's `sub` (`src/musubi/auth/tokens.py`). A presence is the authoring subject of episodic memories and the *from* / *to* of thoughts. One tenant has several presences.
- **Tenant** — The agent identity that owns memory: the continuous "who" across channels (for example `alex`). Each tenant owns a set of presences. The lifecycle worker writes as its own tenant, `lifecycle-worker` (for example `lifecycle-worker/ops/curated`). Pre-v1.0 used the human operator as the tenant; [[13-decisions/0030-agent-as-tenant|ADR 0030]] retired that.
- **Canonical API** — The single HTTP surface exposed by Musubi Core (`openapi.yaml` at the repo root). Every interface (MCP, LiveKit, OpenClaw) consumes this API. See [[07-interfaces/canonical-api]].
- **Adapter** — A client that translates between a specific protocol (MCP, LiveKit tool, OpenClaw extension) and the canonical API. The MCP server (`src/musubi/adapters/mcp`) ships in this repo; LiveKit, OpenClaw and the other agent integrations are separate repos. See [[07-interfaces/index]].
- **SDK** — The Python client library adapters embed (`src/musubi/sdk`, also published as `sourceblender/musubi-sdk`). Hides HTTP details, handles auth, retries, and error types. See [[07-interfaces/sdk]].

## Memory object terms

- **Episodic Memory** — A time-indexed, source-first recollection. "Admin said X to Claude Code at T." See [[04-data-model/episodic-memory]].
- **Curated Knowledge** — A topic-first durable fact. Stored as markdown in the Obsidian vault. Indexed in Qdrant. See [[04-data-model/curated-knowledge]].
- **Source Artifact** — A raw document, transcript, or file. Blob-stored with chunk-level Qdrant index. See [[04-data-model/source-artifact]].
- **Synthesized Concept** — A higher-order memory created by the Lifecycle Engine when multiple episodic memories reinforce the same idea. Bridge between episodic and curated. See [[04-data-model/synthesized-concept]].
- **Thought** — A durable inter-presence message. Not memory per se; it lives in its own `thought` plane (`musubi_thought` collection) on the same infrastructure. See [[04-data-model/thoughts]].

## Lifecycle terms

- **Provisional** — An episodic memory just ingested. Not eligible for deep retrieval yet. TTL-bound.
- **Matured** — A memory that survived the first maturation pass (dedup, importance scoring, tagging).
- **Promoted** — A memory or concept that has been written into the curated vault as a new markdown file.
- **Demoted** — A memory that failed reinforcement checks; removed from default retrieval, kept for provenance.
- **Archived** — Cold-storage state; not queryable in normal retrieval; still in snapshot backups.
- **Superseded** — A memory replaced by a newer version; old version retained via `supersedes` / `superseded_by`.
- **Merged** — A memory created by combining multiple sources; lineage tracked via `merged_from`.
- **Reinforced** — A memory re-validated by a new ingestion; `reinforcement_count` increments, `last_reinforced_at` updates.

## Retrieval terms

- **Fast path** — Latency-budgeted retrieval used by voice agents at turn start: 400 ms whole-call budget by default (`RETRIEVAL_FAST_WHOLE_TIMEOUT_S`), `matured` and `promoted` states only, no reranker, a 30 s response cache. See [[05-retrieval/fast-path]].
- **Deep path** — Full scoring + hybrid retrieval + optional cross-plane fusion. Milliseconds-to-seconds. See [[05-retrieval/deep-path]].
- **Blended retrieval** — Query that returns results from multiple planes fused into a single ranked list. See [[05-retrieval/blended]].
- **Orchestration query** — A compound retrieval that issues subqueries across planes and merges programmatically (e.g., "find episodic memories about X, pull the linked artifact chunks, fetch the curated topic page"). See [[05-retrieval/orchestration]].
- **Hybrid search** — Qdrant query combining dense vectors (BGE-M3) and sparse vectors (SPLADE v3) with server-side RRF fusion. Default for all deep-path retrieval. See [[05-retrieval/hybrid-search]].

## Scoring terms

The score is a weighted sum of five components (`src/musubi/retrieve/scoring.py`).

- **Relevance** — The hybrid (RRF) score, or the reranker score when reranking ran, normalised within the result set.
- **Recency** — Time-decayed weight favoring recent memories, with a per-plane half-life. See [[05-retrieval/scoring-model#recency]].
- **Importance** — LLM-rated 1–10 score assigned at ingestion or maturation. Stanford Generative Agents lineage.
- **Provenance** — Weight derived from the plane and lifecycle state (for example matured curated > promoted concept > matured episodic > provisional episodic). This is where maturity enters the score.
- **Reinforcement** — Log-scaled count of re-ingestion / re-access events.

## Infrastructure terms

- **Qdrant** — The vector DB. Version 1.17 ([[13-decisions/0023-qdrant-version-bump-to-1-17|ADR 0023]]). See [[08-deployment/qdrant-config]].
- **Obsidian Vault** — The filesystem-rooted markdown corpus. Store of record for curated knowledge.
- **Lifecycle Engine** — Background worker process (`lifecycle-worker`) that runs maturation, synthesis, promotion, demotion, reflection and vault reconciliation. Separate from the API server. See [[06-ingestion/lifecycle-engine]].
- **Canonical asset** — Data that must be backed up because it cannot be rebuilt from anywhere else. See [[09-operations/asset-matrix]].
- **Derived asset** — Data that can be regenerated from canonical sources. Backup is optional but may speed up recovery.

## Not Musubi terms (contrast)

- **"Short-term memory" / "long-term memory"** — Terms used loosely in agent literature. Musubi does not use them directly; instead, lifecycle states (provisional / matured / promoted) carry the semantics explicitly.
- **"Vector DB" / "RAG store"** — Musubi is a *memory system that uses* a vector DB. It is not just a RAG store.
