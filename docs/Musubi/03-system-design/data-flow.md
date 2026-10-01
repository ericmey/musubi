---
title: Data Flow
section: 03-system-design
tags: [architecture, data-flow, section/system-design, sequence, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[03-system-design/index]]"
reviewed: false
implements: "docs/Musubi/03-system-design/"
---
# Data Flow

Sequence diagrams for the primary operations. All diagrams are ASCII so they round-trip through Obsidian.

> The stack is defined in the root `docker-compose.yml`, with an optional GPU overlay in `deploy/docker/compose.local-gpu.yml`. "TEI" below means the dense, sparse and reranker services, which are three separate endpoints (`TEI_DENSE_URL`, `TEI_SPARSE_URL`, `TEI_RERANKER_URL`). See [[08-deployment/compose-stack]].

## 1. Episodic capture (hot path)

```
Adapter             Core              TEI dense / sparse      Qdrant
  │                   │                     │                   │
  │ POST /v1/episodic │                     │                   │
  ├──────────────────►│                     │                   │
  │                   │ auth + validate     │                   │
  │                   │ embed dense         │                   │
  │                   ├────────────────────►│                   │
  │                   │ embed sparse        │                   │
  │                   ├────────────────────►│                   │
  │                   │◄────────────────────┤                   │
  │                   │                                         │
  │                   │ nearest neighbour in the same namespace │
  │                   ├────────────────────────────────────────►│
  │                   │◄────────────────────────────────────────┤
  │                   │                                         │
  │                   │ similarity ≥ 0.92 and compatible:       │
  │                   │   merge into the existing row           │
  │                   │   (reinforcement_count + 1)             │
  │                   │ else: upsert a new provisional point    │
  │                   ├────────────────────────────────────────►│
  │                   │◄────────────────────────────────────────┤
  │ 2xx {object_id, state, ...}                                 │
  │◄──────────────────┤                                         │
```

The 0.92 dedup threshold is `_DEFAULT_DEDUP_THRESHOLD` in `src/musubi/planes/episodic/plane.py`. Latency figures for the measured reference host are in [[09-operations/capacity]].

## 2. Deep and blended retrieval

```
Adapter             Core                       TEI            Qdrant        TEI reranker
  │                   │                         │                │               │
  │ POST /v1/retrieve │                         │                │               │
  │ {mode: deep |     │                         │                │               │
  │  blended,         │                         │                │               │
  │  query_text,      │                         │                │               │
  │  namespace,       │                         │                │               │
  │  planes, limit}   │                         │                │               │
  ├──────────────────►│                         │                │               │
  │                   │ auth + scope check      │                │               │
  │                   │ embed query dense+sparse│                │               │
  │                   ├────────────────────────►│                │               │
  │                   │◄────────────────────────┤                │               │
  │                   │                                          │               │
  │                   │ per plane, concurrently: hybrid query    │               │
  │                   │ (server-side RRF), limit × 2 candidates  │               │
  │                   ├─────────────────────────────────────────►│               │
  │                   │◄─────────────────────────────────────────┤               │
  │                   │                                                          │
  │                   │ rerank candidates (skipped for ≤ 5)                      │
  │                   ├─────────────────────────────────────────────────────────►│
  │                   │◄─────────────────────────────────────────────────────────┤
  │                   │                                                          │
  │                   │ score: weighted(relevance, recency, importance,          │
  │                   │   provenance, reinforcement)                             │
  │                   │ blended: merge planes into one ranked list               │
  │                   │ hydrate lineage; increment access_count                  │
  │                   │                                                          │
  │ 200 {results: [...], warnings: [...]}                                        │
  │◄──────────────────┤                                                          │
```

`mode` is one of `fast`, `deep`, `blended` or `recent` (`src/musubi/api/routers/retrieve.py`). A reranker failure or timeout (default budget 1.5 s) falls back to the hybrid order and adds a `reranker_failed` warning. `POST /v1/retrieve/stream` streams the same results.

## 3. Fast path (voice context on a prefetch-cache miss)

```
LiveKit integration         Core (POST /v1/retrieve, mode=fast)         Qdrant
      │                                 │                                 │
      │ prefetch cache miss             │                                 │
      ├────────────────────────────────►│                                 │
      │                                 │ in-process response cache       │
      │                                 │ (exact query, 30 s TTL)         │
      │                                 │                                 │
      │                                 │ on miss: embed query, hybrid    │
      │                                 │ query per plane, states matured │
      │                                 │ and promoted only, NO rerank    │
      │                                 ├────────────────────────────────►│
      │                                 │◄────────────────────────────────┤
      │                                 │ score, cache the response       │
      │ 200 {results, warnings}         │                                 │
      │◄────────────────────────────────┤                                 │
```

Budgets (`src/musubi/settings.py`): 250 ms for query encoding, 250 ms per plane, 400 ms for the whole call. A plane that misses its budget is dropped with a `plane_timeout_<plane>` warning. See [[05-retrieval/fast-path]].

## 4. Artifact ingest

```
Adapter         Core                         Blob dir        Lifecycle worker      TEI      Qdrant
  │               │                             │                  │                 │         │
  │ POST          │                             │                  │                 │         │
  │ /v1/artifacts │                             │                  │                 │         │
  │ (multipart)   │                             │                  │                 │         │
  ├──────────────►│                             │                  │                 │         │
  │               │ stream to staging, sha256,  │                  │                 │         │
  │               │ enforce ARTIFACT_MAX_BYTES  │                  │                 │         │
  │               │ create metadata row ──────────────────────────────────────────────────────►│
  │               │ move blob into place ──────►│                  │                 │         │
  │               │ enqueue durable index intent                   │                 │         │
  │ 2xx {object_id, state, sha256, size_bytes}  │                  │                 │         │
  │◄──────────────┤                             │                  │                 │         │
  │               │                             │ claim intent     │                 │         │
  │               │                             │◄─────────────────┤                 │         │
  │               │                             │ chunk + embed ───────────────────►│         │
  │               │                             │ stage chunks, publish generation ─────────►│
```

Upload returns as soon as the blob and metadata are stored; chunking and embedding run in the lifecycle worker through the lifecycle coordinator (`src/musubi/planes/artifact/indexer.py`). Poll `GET /v1/artifacts/{object_id}` for the indexing state (`indexing`, `indexed`, `failed`, `stored_unindexed`).

## 5. Curated vault file edit (human)

```
Human in Obsidian      Vault (filesystem)        Lifecycle worker (vault_reconcile, every 6 h)       Qdrant
      │                       │                              │                                       │
      │ saves a .md file      │                              │                                       │
      ├──────────────────────►│                              │                                       │
      │                       │  scan the vault              │                                       │
      │                       │◄─────────────────────────────┤                                       │
      │                       │                              │ skip hidden and `_`-prefixed dirs     │
      │                       │                              │ parse + validate frontmatter          │
      │                       │                              │ skip files with no object_id          │
      │                       │                              │ unchanged body hash → skip            │
      │                       │                              │ changed → embed + upsert ────────────►│
      │                       │                              │ row whose file is gone → archive ────►│
```

There is no real-time watcher in the shipped stack; `src/musubi/vault/watcher.py` exists as a module but no service runs it. Invalid frontmatter is logged and counted as `errored`, and the index entry stays unchanged.

## 6. Concept synthesis (lifecycle worker, daily 03:00 UTC)

```
  synthesis job fires
         │
         ▼
  discover tenants (identity families) present in musubi_episodic
         │
         ▼
  for each tenant:
         │ select matured episodic memories since the last cursor
         ▼
  cluster by dense-vector cosine (threshold 0.70), keep clusters of ≥ 3
         │
         ▼
  for each cluster: LLM (LLM_MODEL) drafts a concept
         │
         │ search existing concepts in <tenant>/shared/concept
         ▼
  ├── similarity ≥ 0.85 → reinforce the existing concept
  └── otherwise         → create a concept (state=synthesized, merged_from=[episodic ids])
         │
         ▼
  contradiction check between concepts with similarity in [0.75, 0.85)
```

Thresholds are the defaults in `src/musubi/lifecycle/synthesis.py`. The `concept_maturation` job (daily 03:30) then moves synthesized concepts without active contradictions to `matured`.

## 7. Promotion (lifecycle worker, daily 04:00 UTC)

```
  promotion job fires
         │
         ▼
  query concepts where state=matured and the gate passes:
         │   reinforcement_count ≥ 3, importance ≥ 6, age ≥ 48 h,
         │   no contradictions, promotion_attempts < 3, not yet promoted
         ▼
  LLM renders curated markdown
         │
         ▼
  compute path: curated/<tenant>/<presence>/<topic>/<slug>.md
         │   existing musubi-managed file from this concept → rewrite in place
         │   any other existing file → write a sibling, emit an ops-alerts thought
         ▼
  VaultWriter.write_curated (write log first, then atomic write)
         │
         ▼
  index the curated row in Qdrant; concept.state = promoted, promoted_to set
```

See [[06-ingestion/promotion]].

## 8. Voice agent blended recall (LiveKit integration)

```
  user is speaking: each transcript segment
         │  Slow Thinker cancels any in-flight prefetch and starts a new
         │  mode="deep" retrieve on the transcript so far; results go to a cache
         ▼
  user turn completes
         │  one final mode="deep" prefetch on the full utterance
         ▼
  agent needs context (Fast Talker get_context)
         │  1. read the prefetch cache (similarity match on the query)
         │  2. hit  -> use the cached deep results and their warnings
         │     miss -> mode="fast" retrieve (400 ms budget); on error, []
         ▼
  agent speaks
         │  maybe_capture_fact(utterance): heuristic episodic capture,
         │  when the integration calls it (capture_facts, on by default)
         ▼
  session ends (capture_transcripts, on by default)
         │  upload the transcript as an artifact (falls back to an
         │  episodic capture), then send a session-summary thought
```

Fast and deep do not start in parallel: deep runs ahead as a prefetch while the user speaks, and fast is used only on a cache miss. This logic lives in `sourceblender/musubi-livekit` (`adapter.py`, `slow_thinker.py`, `fast_talker.py`). See [[07-interfaces/livekit-adapter]] and [[05-retrieval/fast-path]].

## Test Contract

This is an architecture-overview spec — no single code path or test file owns it end-to-end. Verification is distributed across the component specs in sections 04–10, each of which carries its own `## Test Contract` section.
