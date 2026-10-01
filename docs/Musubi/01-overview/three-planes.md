---
title: The Three Planes
section: 01-overview
tags: [architecture, overview, planes, section/overview, status/complete, type/overview]
type: overview
status: complete
updated: 2026-10-01
up: "[[01-overview/index]]"
reviewed: false
---
# The Three Planes

The core mental model. Every design decision in this vault reduces to: "which plane does this belong in?"

## Plane 1 — Episodic Memory

**What:** A time-indexed, source-first recollection. Something happened, someone said it, at a specific time. Modality-agnostic — it might be a chat message, a voice turn, a chat-server post, a tool-call result.

**Primary question it answers:** "What has happened recently / ever between this presence and this person?"

**Truth model:** High-recall, high-noise. Most episodes are low-importance ambient chatter; a few are critical.

**Store:** Qdrant `musubi_episodic`, one collection for all namespaces (the namespace is a payload field filtered at query time), with BGE-M3 dense and SPLADE v3 sparse named vectors.

**Write path:** Low-latency capture (`POST /v1/episodic`). Provisional state. The lifecycle worker matures it later.

**Retention:** Lifecycle-driven. Provisional memories are archived after 7 days unless matured (`provisional_ttl` sweep). Matured memories retained indefinitely unless demoted.

**Retrieval characteristics:** Fast path (latency-budgeted, no reranker, short-lived cache), deep path (hybrid + scored + optional rerank).

See [[04-data-model/episodic-memory]] for schema.

## Plane 2 — Curated Knowledge

**What:** A durable, topic-first fact or concept. "The admin's preferred coding style is X." "Musubi uses BGE-M3 for dense embeddings." Authoritative.

**Primary question it answers:** "What does this team hold as true about this topic?"

**Truth model:** Human-authored or human-approved. Low noise. Stable.

**Store:** **Obsidian vault** (`VAULT_PATH`) is the store of record: markdown files with YAML frontmatter. Promotion writes to `curated/<tenant>/<presence>/<topic>/<slug>.md` (`src/musubi/lifecycle/promotion.py`). Qdrant (`musubi_curated`) is a derived index mirroring the vault; it can be rebuilt from scratch from the vault.

**Write path:** Primary write is a human editing a markdown file in Obsidian. The lifecycle worker's `vault_reconcile` job re-indexes the vault every 6 hours. A real-time watcher module exists (`src/musubi/vault/watcher.py`) but no service runs it. Musubi may *also* write files (promotions from synthesis), but it never overwrites a file that lacks `musubi-managed: true`; on a conflict it writes a sibling file.

**Retention:** Indefinite. Musubi does not delete vault files.

**Retrieval characteristics:** Deep path with high provenance weight. Curated results rank higher than episodic at equal relevance.

See [[04-data-model/curated-knowledge]] for schema.

## Plane 3 — Source Artifact

**What:** Raw material. A 30-minute call transcript. A 200-page PDF. A chat channel export. The canonical thing that Curated Knowledge might be *about*.

**Primary question it answers:** "Show me the source. What are the exact words?"

**Truth model:** Ground truth. Never mutated. Additive only.

**Store:**
- The original bytes on disk under `ARTIFACT_BLOB_PATH/<namespace>/<object_id>`, with a recorded SHA-256.
- Qdrant `musubi_artifact` for artifact metadata ([[13-decisions/0009-artifact-metadata-in-qdrant|ADR 0009]]).
- Qdrant `musubi_artifact_chunks` for the chunk-level dense + sparse index.

**Write path:** Artifact is uploaded to `POST /v1/artifacts` (multipart) with metadata, chunked (structure-aware — headings for markdown, speaker turns for transcripts), embedded, indexed. Never modified after ingestion; re-ingestion creates a new artifact version.

**Retention:** Indefinite. Archival is a state change; blob bytes stay. Only an explicit purge (`POST /v1/artifacts/{object_id}/purge`) removes the blob.

**Retrieval characteristics:** Deep RAG path. Typically chained from episodic or curated retrieval ("find the chunk this claim came from").

See [[04-data-model/source-artifact]] for schema.

## The bridge layer — Synthesized Concept Memory

**What:** A higher-order memory that emerges when multiple episodic memories converge on the same idea. Created by the [[06-ingestion/concept-synthesis|synthesis job]] in the lifecycle worker. Example: five separate episodic memories of the Admin mentioning different aspects of "CUDA 13 setup" → one synthesized concept `CUDA 13 setup notes` linked to all five.

**Why it's a separate type, not just curated:**
- Synthesized concepts are *system-generated hypotheses*, not human-authoritative facts.
- They have lower provenance weight than curated.
- They are candidates for promotion; not all make it.
- Distinguishing them lets the scorer treat them appropriately.

**Store:** Qdrant (`musubi_concept`), with links to the episodic IDs they were synthesized from (`merged_from`) and, if promoted, the curated file that resulted (`promoted_to`).

**Write path:** Synthesis job only. Humans do not write concepts directly — they write curated knowledge.

**Promotion:** New concepts start `synthesized`; the daily `concept_maturation` job moves them to `matured`. A concept is promoted only when it is `matured` with ≥ 3 reinforcements, importance ≥ 6, age ≥ 48 h, no contradictions and fewer than 3 failed promotion attempts (`src/musubi/lifecycle/promotion.py`). Promotion writes a `musubi-managed: true` file to the vault and updates the concept's `state` to `promoted`. See [[06-ingestion/promotion]].

See [[04-data-model/synthesized-concept]] for schema.

## How the planes interact

```
 CAPTURE            MATURE               SYNTHESIZE             PROMOTE
 ───────            ──────               ──────────             ───────
 POST /v1/episodic  hourly at :13        daily 03:00,           daily 04:00,
                                         concept matured 03:30  thresholds
 provisional  ────► matured  ──────────► concept  ────────────► curated
 episodic           episodic             (synthesized →         (vault file)
                                          matured)


ARTIFACT FLOW
────────────
POST /v1/artifacts  ──►  blob saved  ──►  chunked  ──►  embedded  ──►  registered
                                                                          (ID + chunks in Qdrant)

                                           any memory can link to an artifact
                                           via `supported_by` (artifact id + optional chunk id)
```

Plane-crossing links are the key data structure. A curated knowledge file cites artifact chunks. An episodic memory can cite an artifact chunk. A synthesized concept cites the episodic memories it was merged from.

See [[04-data-model/relationships]] for the full relationship catalog.

## Why not a knowledge graph

Zep/Graphiti build a knowledge graph as the primary store. We considered and rejected this at the current scale:

- A small team's knowledge-graph density is low; the KG overhead dominates benefit.
- Our synthesis + promotion pipeline already captures "this fact was derived from these sources" via lineage fields — a lighter-weight version of edges.
- Obsidian's wikilinks already give us a human-readable graph view of curated knowledge; we don't need to replicate that in Qdrant.
- If we outgrow this, [[13-decisions/0004-no-knowledge-graph-v1]] documents the exit path: add a Neo4j or SQLite-backed edge store alongside Qdrant, populated by the lifecycle worker.
