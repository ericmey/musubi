---
title: "ADR 0004: No Knowledge Graph in v1"
section: 13-decisions
tags: [adr, knowledge-graph, scope, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0004: No Knowledge Graph in v1

**Status:** accepted
**Date:** 2026-03-15
**Deciders:** Eric

## Context

Several prior-art systems build an explicit entity-relationship graph alongside (or instead of) a vector store:

- **Zep** uses a bitemporal relational graph with entities, facts, relationships; retrieval traverses the graph.
- **Microsoft GraphRAG** extracts entities from documents, builds a graph with community detection, queries communities as units.
- **Stanford Generative Agents** build an implicit graph via reflection-generated memories that reference prior memories.

The appeal: you can ask "what does Musubi know about Alice?" and get a structured view — relationships, facts, changes over time — rather than a pile of similar-embedding hits.

The cost: building, maintaining, and querying a graph is a real project. Entity extraction is error-prone; relationship inference more so. You acquire a whole second retrieval path (graph traversal) on top of vector search. And you likely need a second store (neo4j, kuzu, or a graph layer on top of a relational DB).

For household/small-team scale, the *symptom* that a graph would solve — "retrieval misses obvious connections" — hasn't actually shown up in the POC. Hybrid dense+sparse+rerank ([[13-decisions/0005-hybrid-search]]) covers most of what naive graph traversal would, at much less cost.

## Decision

**No knowledge graph in v1.** Stick with planes + vector retrieval + payload metadata (tags, presences, projects). If graph-shaped queries become necessary, reconsider for v2+.

What we do instead:

- Tags and presences in payload serve as a flat category system (queries can filter `tags=["projects/livekit"]`).
- Wiki-links between vault files serve as a human-maintained knowledge graph that the vector index respects implicitly (a link in curated doc A to curated doc B is still just text in the chunk).
- Synthesis ([[06-ingestion/concept-synthesis]]) produces concept documents that summarize relationships. This is a soft, *learned* graph rather than a hard, *extracted* one.

## Alternatives

**A. Build an entity extractor + graph store (Zep-style).** Much more work; benefits unclear at our scale.

**B. Use Obsidian's own graph (wiki-links).** We kind of already do — but that's only in the vault (curated plane), and it's just hyperlinks, not a queryable store. That's fine.

**C. Implicit graph via shared tags.** Retrieval that filters by tag is roughly a "find everything in this neighborhood" query. This is what we have. Good enough.

**D. GraphRAG-style community detection.** Heavyweight; requires LLM passes over the whole corpus periodically. Not worth it for a personal-scale system in v1.

## Consequences

- Scope for v1 stays manageable — no graph infra to build or operate.
- Retrieval quality depends heavily on embedding quality + rerank. We're okay with that; those are already best-in-class as of 2026.
- Some queries that a graph would answer naturally (e.g., "who was at the March 12 meeting?") will rely on tag+time filters + vector search. Occasionally worse than a graph. Acceptable for now.
- Re-opening this decision: if synthesis starts routinely producing high-confidence relationships the system can't surface via search, that's the signal.

Trade-offs:

- Multi-hop queries (A is related to B is related to C) will be weak. In practice, users don't phrase queries that way at this scale.

## Links

- [[13-decisions/0005-hybrid-search]]
- [[06-ingestion/concept-synthesis]]
- [[12-roadmap/phased-plan]]
