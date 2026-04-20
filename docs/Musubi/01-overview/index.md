---
title: "01 — Overview"
section: 01-overview
tags: [overview, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# 01 — Overview

This section frames Musubi: what it is, who it serves, the problem it solves, and the three-plane model that shapes every other section.

## Documents in this section

- [[01-overview/mission]] — What Musubi is for.
- [[01-overview/three-planes]] — The core mental model.
- [[01-overview/personas]] — Who uses it and how.
- [[01-overview/non-goals]] — What we explicitly do not build.
- [[01-overview/research-grounding]] — Which external systems shape this design and how we diverge.

## Summary

Musubi (結び — "knot," "tie together") is a **shared memory and knowledge plane for a small-team AI agent fleet**. It runs as a standalone server on a dedicated, GPU-equipped Ubuntu host. Every AI interface a human uses — Claude Code via MCP, a LiveKit voice agent, the OpenClaw desktop app, a shell script calling REST — shares memory through Musubi.

The system is built around three planes with different truth models:

1. **Episodic Plane** — who said what, when. Fast and source-first. Lives in Qdrant.
2. **Curated Knowledge Plane** — durable, topic-first facts. The Obsidian vault *is* this plane; Qdrant indexes it.
3. **Source Artifact Plane** — raw documents, transcripts, logs. Ground truth. Blob-stored with chunk-level index.

A bridge layer, **Synthesized Concept Memory**, emerges from reinforcement patterns in the episodic plane and is the path by which knowledge flows upward into curated form.

## Why three planes

A single-plane memory system forces tradeoffs:

- If the plane is optimized for recent turns (short TTL, low-importance filter off), long-term retrieval suffers.
- If the plane is optimized for curated facts (high importance bar, strong dedup), episodic continuity is lost.
- If it is optimized for RAG over whole documents, neither individual turns nor topical facts are cheap to retrieve.

Separate planes let each layer have the right write path, retention, scoring weights, and operator controls. The trade-off is that cross-plane retrieval requires an [[05-retrieval/orchestration|orchestration layer]] — but that is a concept we control, not a muddled single collection.

## Why Obsidian is the curated store of record

The curated plane is where a human operator actually sits and thinks: editing notes, refining topics, writing authoritative summaries. Forcing that workflow through a web UI we'd have to build is worse than letting a human use the best existing tool (Obsidian) and making Musubi watch.

Consequences:

- Every curated memory is a human-readable markdown file with YAML frontmatter.
- The human can edit in Obsidian while Musubi is running; changes propagate via file watcher.
- Backup of curated knowledge is git. Disaster recovery of the curated index is `rebuild from vault`.
- Musubi can still *write* curated knowledge (promotions from synthesis), but only into files marked `musubi-managed: true`.

See [[13-decisions/0003-obsidian-as-sor]] for the full rationale and alternatives.

## What changed from the POC

The current POC (see [[02-current-state/index]]) is a two-collection Qdrant + MCP server. It conflates the three planes into one `musubi_memories` collection and has no curated / artifact distinction. This redesign:

- Splits collections by plane and introduces named vectors.
- Moves from MCP-as-server to canonical-API-as-server + MCP-as-adapter.
- Introduces a Lifecycle Engine as a separate worker process.
- Makes all inference local (BGE-M3, SPLADE++, local reranker, local LLM for importance/synthesis).
- Adds explicit versioning, lineage, and no-silent-mutation discipline.
