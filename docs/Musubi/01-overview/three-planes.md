---
title: The Three Planes
section: 01-overview
tags: [architecture, overview, planes, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-04-17
up: "[[01-overview/index]]"
reviewed: false
---
# The Three Planes

The core mental model. Every design decision in this vault reduces to: "which plane does this belong in?"

## Plane 1 — Episodic Memory

**What:** A time-indexed, source-first recollection. Something happened, someone said it, at a specific time. Modality-agnostic — it might be a chat message, a voice turn, a Discord post, a tool-call result.

**Primary question it answers:** "What has happened recently / ever between this presence and this person?"

**Truth model:** High-recall, high-noise. Most episodes are low-importance ambient chatter; a few are critical.

**Store:** Qdrant (`musubi_episodic_{namespace}`), with local BGE-M3 dense + SPLADE++ sparse named vectors.

**Write path:** Low-latency capture (< 10ms local embed). Provisional state. Lifecycle engine matures later.

**Retention:** Lifecycle-driven. Provisional memories TTL after 7 days unless matured. Matured memories retained indefinitely unless demoted.

**Retrieval characteristics:** Fast path (episodic-only, cached), deep path (hybrid + scored).

See [[04-data-model/episodic-memory]] for schema.

## Plane 2 — Curated Knowledge

**What:** A durable, topic-first fact or concept. "Eric's preferred coding style is X." "Musubi uses BGE-M3 for dense embeddings." Authoritative.

**Primary question it answers:** "What does this team hold as true about this topic?"

**Truth model:** Human-authored or human-approved. Low noise. Stable.

**Store:** **Obsidian vault** is the store of record. Files in `vault/curated/<topic>/<slug>.md` with YAML frontmatter. Qdrant (`musubi_curated`) is a derived index mirroring the vault; it can be rebuilt from scratch via the vault sync.

**Write path:** Primary write is a human editing a markdown file in Obsidian. Musubi watches via Watchdog, reindexes on change. Musubi may *also* write files (promotions from synthesis) but only those tagged `musubi-managed: true`, and never edits files tagged `musubi-managed: false`.

**Retention:** Indefinite. Demotions move files to `vault/_archive/`; they stay on disk.

**Retrieval characteristics:** Deep path with high provenance weight. Curated results rank higher than episodic at equal relevance.

See [[04-data-model/curated-knowledge]] for schema.

## Plane 3 — Source Artifact

**What:** Raw material. A 30-minute call transcript. A 200-page PDF. A Discord channel export. The canonical thing that Curated Knowledge might be *about*.

**Primary question it answers:** "Show me the source. What are the exact words?"

**Truth model:** Ground truth. Never mutated. Additive only.

**Store:**
- Blob storage for the original file (`vault/artifacts/<id>/<filename>`).
- Qdrant (`musubi_artifact_chunks`) for chunk-level dense + sparse index.
- Metadata row in Musubi (artifact registry) linking blob, chunk IDs, and ingestion metadata.

**Write path:** Artifact is POSTed to Musubi with metadata, chunked (structure-aware — headings for markdown, speaker turns for transcripts), embedded, indexed. Never modified after ingestion; re-ingestion creates a new artifact version.

**Retention:** Indefinite. Demotions/archival are metadata-only flags; blobs stay.

**Retrieval characteristics:** Deep RAG path. Typically chained from episodic or curated retrieval ("find the chunk this claim came from").

See [[04-data-model/source-artifact]] for schema.

## The bridge layer — Synthesized Concept Memory

**What:** A higher-order memory that emerges when multiple episodic memories converge on the same idea. Created by the [[06-ingestion/concept-synthesis|synthesis job]] in the Lifecycle Engine. Example: five separate episodic memories of Eric mentioning different aspects of "CUDA 13 setup" → one synthesized concept `CUDA 13 setup notes` linked to all five.

**Why it's a separate type, not just curated:**
- Synthesized concepts are *system-generated hypotheses*, not human-authoritative facts.
- They have lower provenance weight than curated.
- They are candidates for promotion; not all make it.
- Distinguishing them lets the scorer treat them appropriately.

**Store:** Qdrant (`musubi_concepts`), with links to the episodic IDs they were synthesized from (`merged_from`) and, if promoted, the curated file that resulted (`promoted_to`).

**Write path:** Synthesis job only. Humans do not write concepts directly — they write curated knowledge.

**Promotion:** A concept is promoted when it has N≥3 reinforcements, importance ≥ 6, age ≥ 48h, and no contradiction. Promotion writes a `musubi-managed: true` file to the vault and updates the concept's `state` to `promoted`. See [[06-ingestion/promotion]].

See [[04-data-model/synthesized-concept]] for schema.

## How the planes interact

```
 CAPTURE                  MATURE                 SYNTHESIZE           PROMOTE
 ──────                   ──────                 ──────────           ───────
Adapter                 Lifecycle               Lifecycle           Lifecycle
 POST     ────►  provisional   ─(hourly)──►   matured  ─(daily)──►  concept  ─(daily, thresholds)──►  curated (vault file)
episodic           episodic                    episodic             (synth)
memory             memory                      memory


ARTIFACT FLOW
────────────
Adapter POST artifact  ──►  blob saved  ──►  chunked  ──►  embedded  ──►  registered
                                                                          (ID + chunks in Qdrant)

                                           any memory can link to an artifact
                                           via `supported_by: [artifact-id#chunk-id]`
```

Plane-crossing links are the key data structure. A curated knowledge file cites artifact chunks. An episodic memory can cite an artifact chunk. A synthesized concept cites the episodic memories it was merged from.

See [[04-data-model/relationships]] for the full relationship catalog.

## Why not a knowledge graph

Zep/Graphiti build a knowledge graph as the primary store. We considered and rejected this at the current scale:

- A small team's knowledge-graph density is low; the KG overhead dominates benefit.
- Our synthesis + promotion pipeline already captures "this fact was derived from these sources" via lineage fields — a lighter-weight version of edges.
- Obsidian's wikilinks already give us a human-readable graph view of curated knowledge; we don't need to replicate that in Qdrant.
- If we outgrow this, [[13-decisions/0004-no-knowledge-graph-v1]] documents the exit path: add a Neo4j or SQLite-backed edge store alongside Qdrant, populated by the Lifecycle Engine.
