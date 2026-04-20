---
title: Data Flow
section: 03-system-design
tags: [architecture, data-flow, section/system-design, sequence, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Data Flow

Sequence diagrams for the primary operations. All diagrams are ASCII so they round-trip through Obsidian.

## 1. Episodic store (hot path)

```
Adapter             Core                TEI              Qdrant
  │                   │                   │                 │
  │  POST /v1/ep/mem  │                   │                 │
  ├──────────────────►│                   │                 │
  │                   │  auth + validate  │                 │
  │                   ├──────────────────►│                 │
  │                   │   /embed dense    │                 │
  │                   │◄──────────────────┤                 │
  │                   │   /embed_sparse   │                 │
  │                   ├──────────────────►│                 │
  │                   │◄──────────────────┤                 │
  │                   │   (both vectors)  │                 │
  │                   │                   │                 │
  │                   │  query nearest (dense) for dedup    │
  │                   ├──────────────────────────────────-─►│
  │                   │◄────────────────────────────────────┤
  │                   │                                     │
  │                   │  if sim ≥ 0.92: batch_update_points │
  │                   │  else:          upsert point        │
  │                   ├────────────────────────────────────►│
  │                   │◄────────────────────────────────────┤
  │ 200 {id, state:provisional, dedup_hit:bool}             │
  │◄──────────────────┤                                     │
```

Budget: < 100ms on the reference host, typical 30–60ms. Dominated by sparse embedding (SPLADE++ is the slowest of the three inference calls).

## 2. Episodic recall (deep path, blended query)

```
Adapter             Core                 TEI                Qdrant            Reranker(TEI)
  │                   │                   │                    │                   │
  │  POST /v1/query   │                   │                    │                   │
  ├──────────────────►│                   │                    │                   │
  │ {query, planes,   │                   │                    │                   │
  │  namespace, n=20} │                   │                    │                   │
  │                   │  auth + validate  │                    │                   │
  │                   │                   │                    │                   │
  │                   │  /embed dense     │                    │                   │
  │                   ├──────────────────►│                    │                   │
  │                   │  /embed_sparse    │                    │                   │
  │                   ├──────────────────►│                    │                   │
  │                   │◄──────────────────┤                    │                   │
  │                   │                                        │                   │
  │                   │  Qdrant Query API (hybrid, server RRF  │                   │
  │                   │   fusion) per plane in parallel:       │                   │
  │                   │   - episodic (n=40, filter=ns)         │                   │
  │                   │   - curated  (n=20, filter=ns)         │                   │
  │                   ├───────────────────────────────────────►│                   │
  │                   │◄───────────────────────────────────────┤                   │
  │                   │                                        │                   │
  │                   │  scoring: weighted(relevance, recency, │                   │
  │                   │   importance, maturity, reinforcement, │                   │
  │                   │   provenance) - penalties              │                   │
  │                   │                                        │                   │
  │                   │  cross-encoder rerank top 60 → top 20  │                   │
  │                   ├──────────────────────────────────────────────────────────►│
  │                   │◄──────────────────────────────────────────────────────────┤
  │                   │                                                           │
  │                   │  increment access_count (batch_update) │                  │
  │                   ├───────────────────────────────────────►│                  │
  │                   │                                                           │
  │ 200 {results:[{id, content, score, plane, breakdown, ...}]}                   │
  │◄──────────────────┤                                                           │
```

Budget: < 250ms p95. Dominated by reranker call (~80ms for 60 candidates on RTX 3080).

## 3. Fast path (LiveKit turn-start prefetch)

```
LiveKit Adapter              Core (fast-path endpoint)            Qdrant + local cache
      │                                 │                                │
      │  on_user_turn_completed hook fires                              │
      │  (last 2–3 user turns available)                                 │
      │                                 │                                │
      │  POST /v1/fast/episodic         │                                │
      │  {namespace, turns, n=5}        │                                │
      ├────────────────────────────────►│                                │
      │                                 │                                │
      │                                 │  check fast-path cache         │
      │                                 │  (in-process LRU, 30s TTL)     │
      │                                 ├───────────────────────────────►│ (miss or stale)
      │                                 │                                │
      │                                 │  /embed dense (truncated input)│
      │                                 │   + Qdrant dense-only query    │
      │                                 │   (NO sparse, NO rerank)       │
      │                                 │   on `musubi_episodic` only    │
      │                                 │   with filter=ns + state=matured│
      │                                 │                                │
      │                                 │  update cache                  │
      │                                 │                                │
      │ 200 {hits: [5 brief episodic memories]}                          │
      │◄────────────────────────────────┤                                │
```

Budget: **< 50ms p95 absolute ceiling**. Typical ~15–30ms on cache miss, < 5ms on hit. No sparse, no rerank, no cross-plane, no importance/recency scoring beyond default.

Cache is keyed by a hash of the last 2 user turns; invalidated by any write to the namespace. See [[05-retrieval/fast-path]].

## 4. Artifact ingest

```
Adapter         Core                       Object Store     TEI              Qdrant
  │               │                            │              │                │
  │ POST          │                            │              │                │
  │ /v1/artifacts │                            │              │                │
  │ (multipart or │                            │              │                │
  │  pre-signed   │                            │              │                │
  │  URL ref)     │                            │              │                │
  ├──────────────►│                            │              │                │
  │               │ compute sha256             │              │                │
  │               │ write blob if new          │              │                │
  │               ├───────────────────────────►│              │                │
  │               │◄───────────────────────────┤              │                │
  │               │                            │              │                │
  │               │ chunk (structure-aware:    │              │                │
  │               │  headings for md,          │              │                │
  │               │  speaker turns for VTT,    │              │                │
  │               │  N tokens otherwise)       │              │                │
  │               │                            │              │                │
  │               │ /embed dense + sparse for  │              │                │
  │               │  each chunk (batched)      │              │                │
  │               ├───────────────────────────────────────────►              │
  │               │◄──────────────────────────────────────────┤                │
  │               │                            │              │                │
  │               │ upsert chunks              │              │                │
  │               ├────────────────────────────────────────────────────────►│
  │               │◄────────────────────────────────────────────────────────┤
  │               │                            │              │                │
  │ 200 {artifact_id, chunk_count, state:indexed}           │                │
  │◄──────────────┤                            │              │                │
```

Budget: minutes for large artifacts. Done async — response returns immediately with `state: indexing`, status queried via `GET /v1/artifacts/{id}`.

## 5. Curated vault file edit (human)

```
Human in Obsidian          Filesystem                Vault Watcher                 Core             Qdrant
      │                         │                           │                        │                │
      │ saves a .md file in     │                           │                        │                │
      │ vault/curated/eric/...  │                           │                        │                │
      ├────────────────────────►│                           │                        │                │
      │                         │ inotify modify event      │                        │                │
      │                         ├──────────────────────────►│                        │                │
      │                         │                           │ debounce 2s            │                │
      │                         │                           │ read file + frontmatter│                │
      │                         │                           │ validate schema        │                │
      │                         │                           │                        │                │
      │                         │                           │ call Core internal:    │                │
      │                         │                           │  curated_reindex_file( │                │
      │                         │                           │    path, content,      │                │
      │                         │                           │    frontmatter)        │                │
      │                         │                           ├───────────────────────►│                │
      │                         │                           │                        │ embed, upsert  │
      │                         │                           │                        ├───────────────►│
      │                         │                           │                        │◄───────────────┤
      │                         │                           │◄───────────────────────┤                │
      │                         │                           │                                         │
      │                         │                           │ log event                               │
```

If `musubi-managed: true` and the file was just written by Core (write-log hit): skip.

If frontmatter is invalid: log error, emit a thought to the `eric/scheduler` channel so the human sees the notification on next session-sync, leave index unchanged.

## 6. Concept synthesis (scheduled, Lifecycle Worker)

```
  APScheduler fires synthesis_run every 6h
         │
         ▼
  Lifecycle Worker
         │
         │ iterate presences
         ▼
  for presence in registry.list():
         │
         │ scroll matured episodic memories (new since last run)
         ▼
  Qdrant scroll (filter: namespace/episodic, state=matured, updated_epoch > last_run)
         │
         │ cluster by vector (HDBSCAN or simple threshold) + topic
         ▼
  clusters
         │
         │ for each cluster of ≥ 3 memories:
         ▼
  call Ollama (qwen2.5:7b-instruct-q4_K_M): "extract salient facts"
         │
         ▼
  for each extracted fact:
    │
    │  semantic search existing concepts (same ns, concept plane)
    ▼
  Qdrant query (concept plane, similarity ≥ 0.85)
    │
    ├── match found → reinforce (reinforcement_count++; batch_update)
    └── no match → create concept (state=synthesized) with merged_from=[episodic_ids]
         │
         ▼
  log synthesis_run completion record with counters
```

Budget: minutes per run. Runs in background; no user-facing latency impact.

## 7. Promotion (scheduled, Lifecycle Worker)

```
  APScheduler fires promotion_run daily
         │
         ▼
  scroll concepts where state=synthesized
         │
         │ for each, evaluate promotion gate:
         │   reinforcement_count ≥ 3
         │   importance ≥ 6
         │   age ≥ 48h
         │   no active contradiction
         ▼
  if gate passes:
         │
         │ call Ollama: "write a curated summary"
         ▼
  generate markdown + frontmatter
         │
         ▼
  call Core internal: promote_concept_to_vault(concept_id, rendered_md)
         │
         │ writes vault/curated/<ns>/<slug>.md with musubi-managed: true
         │ writes write-log entry so vault-watcher skips the echo
         │ sets concept.state = promoted, promoted_to = <vault-path>
         │ reindexes the new curated file into Qdrant
         ▼
  emit scheduler thought to presence `eric/scheduler`:
    "Promoted concept 'CUDA 13 setup' to curated/eric/projects/cuda.md"
```

## 8. Voice agent blended recall (via LiveKit adapter)

```
  LiveKit agent turn begins
         │
         │ in parallel:
         │  - fast-path episodic (< 50ms)
         │  - deep-path blended query (launched, streams back)
         ▼
  agent starts talking using fast-path hits
         │
         │ if deep-path arrives mid-turn: agent's toolset has the new context
         │ if deep-path arrives late: agent completes turn on fast-path
         ▼
  turn ends; adapter POSTs the exchange as episodic memory
```

See [[07-interfaces/livekit-adapter]] for the adapter-side logic and [[05-retrieval/fast-path]] for the budget analysis.

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the per-component slices listed in the sibling specs under this section, each of which carries its own `## Test Contract` section bound to an owning slice.
