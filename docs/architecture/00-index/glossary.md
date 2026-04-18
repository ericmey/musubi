---
title: Glossary
section: 00-index
tags: [reference, section/index, status/complete, type/index, vocabulary]
type: index
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Glossary

Terms used across this vault. If a term is ambiguous in general usage, this file pins down what Musubi means by it.

## Core terms

- **Plane** — One of three top-level memory partitions: Episodic, Curated Knowledge, Source Artifact. Each has its own write path, truth model, and retention policy. See [[01-overview/three-planes]].
- **Namespace** — A scoping identifier that partitions all memory objects. Shape: `{tenant}/{presence}/{plane}` (e.g., `eric/claude-code/episodic`). Always explicit; never defaulted. See [[03-system-design/namespaces]].
- **Presence** — A named AI-agent identity. Not the same as a human user. Examples: `claude-code`, `claude-desktop`, `livekit-voice`, `openclaw`. A presence is the authoring subject of episodic memories and the *from* / *to* of thoughts. Multiple presences may belong to one human.
- **Tenant** — A human identity or shared household. In a small-team deployment there are 1–N tenants, each owning a set of presences.
- **Canonical API** — The single HTTP + gRPC surface exposed by Musubi Core. Every interface (MCP, LiveKit, OpenClaw) consumes this API. See [[07-interfaces/canonical-api]].
- **Adapter** — An independent downstream project that translates between a specific protocol (MCP, LiveKit tool, OpenClaw extension) and the canonical API. Adapters are separate repos. See [[07-interfaces/index]].
- **SDK** — A client library (Python, TypeScript) that adapters embed. Hides HTTP details, handles auth, retries, and error types. See [[07-interfaces/sdk]].

## Memory object terms

- **Episodic Memory** — A time-indexed, source-first recollection. "Eric said X to Claude-code at T." See [[04-data-model/episodic-memory]].
- **Curated Knowledge** — A topic-first durable fact. Stored as markdown in the Obsidian vault. Indexed in Qdrant. See [[04-data-model/curated-knowledge]].
- **Source Artifact** — A raw document, transcript, or file. Blob-stored with chunk-level Qdrant index. See [[04-data-model/source-artifact]].
- **Synthesized Concept** — A higher-order memory created by the Lifecycle Engine when multiple episodic memories reinforce the same idea. Bridge between episodic and curated. See [[04-data-model/synthesized-concept]].
- **Thought** — A durable inter-presence message. Not memory per se, but a separate namespace backed by the same infra. (Existing POC concept, preserved.)

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

- **Fast path** — Latency-budgeted retrieval (< 50ms) used by voice agents at turn start. Episodic-only, cached, no cross-plane orchestration. See [[05-retrieval/fast-path]].
- **Deep path** — Full scoring + hybrid retrieval + optional cross-plane fusion. Milliseconds-to-seconds. See [[05-retrieval/deep-path]].
- **Blended retrieval** — Query that returns results from multiple planes fused into a single ranked list. See [[05-retrieval/blended]].
- **Orchestration query** — A compound retrieval that issues subqueries across planes and merges programmatically (e.g., "find episodic memories about X, pull the linked artifact chunks, fetch the curated topic page"). See [[05-retrieval/orchestration]].
- **Hybrid search** — Qdrant query combining dense vectors (Gemini) + sparse vectors (BM25) with server-side fusion. Default for all deep-path retrieval. See [[05-retrieval/hybrid-search]].

## Scoring terms

- **Relevance** — Cosine similarity between query vector and memory vector (hybrid score).
- **Recency** — Time-decayed weight favoring recent memories. See [[05-retrieval/scoring-model#recency]].
- **Importance** — LLM-rated 1–10 score assigned at ingestion or maturation. Stanford Generative Agents lineage.
- **Maturity** — Lifecycle state weight (provisional < matured < promoted).
- **Reinforcement** — Log-scaled count of re-ingestion / re-access events.
- **Provenance strength** — Weight derived from source type (curated > synthesized > matured-episodic > provisional-episodic).
- **Duplication penalty** — Reduces score for near-duplicates within a result set.
- **Contradiction penalty** — Reduces score for memories that contradict higher-priority memories in the current namespace.

## Infrastructure terms

- **Qdrant** — The vector DB. Currently 1.15+ (April 2026). See [[08-deployment/qdrant-config]].
- **Obsidian Vault** — The filesystem-rooted markdown corpus. Store of record for curated knowledge.
- **Lifecycle Engine** — Background worker process that runs maturation, synthesis, promotion, demotion. Separate from the API server. See [[06-ingestion/lifecycle-engine]].
- **Canonical asset** — Data that must be backed up because it cannot be rebuilt from anywhere else. See [[09-operations/asset-matrix]].
- **Derived asset** — Data that can be regenerated from canonical sources. Backup is optional but may speed up recovery.

## Not Musubi terms (contrast)

- **"Short-term memory" / "long-term memory"** — Terms used loosely in agent literature. Musubi does not use them directly; instead, lifecycle states (provisional / matured / promoted) carry the semantics explicitly.
- **"Vector DB" / "RAG store"** — Musubi is a *memory system that uses* a vector DB. It is not just a RAG store.
