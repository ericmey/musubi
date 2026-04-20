---
title: Migration
section: 11-migration
tags: [index, migration, section/migration, status/complete, type/migration-phase]
type: migration-phase
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Migration

Moving from POC to v1, and from v1 to whatever's next. Phased so each step is safe, observable, and reversible.

## Starting point: POC

Today's Musubi is a working POC:

- FastMCP server with 4 tools (`memory_store`, `memory_recall`, `thought_send`, etc.).
- Single Qdrant collection (`musubi`), single dense vector, no sparse, no reranker.
- Gemini embeddings via network.
- No vault, no concepts, no lifecycle engine, no artifacts, no canonical HTTP API.

This doc section lays out the sequence of changes to reach v1 without losing data or breaking usage along the way.

## Target: v1

Everything described across sections 01-10 of this vault:

- Canonical HTTP/gRPC API.
- Three planes + synthesized concepts + artifacts.
- Obsidian vault as curated source of truth.
- Hybrid retrieval (BGE-M3 + SPLADE++ V3 + reranker).
- Local inference on dedicated GPU.
- Independent adapters for MCP, LiveKit, OpenClaw.
- Lifecycle worker, contradictions, promotions, reflections.

## Migration phases (live)

```dataview
TABLE WITHOUT ID
  file.link AS "Phase",
  status AS "Status",
  prev AS "Prev",
  next AS "Next",
  updated AS "Updated"
FROM "11-migration"
WHERE type = "migration-phase" AND startswith(file.name, "phase-")
SORT file.name ASC
```

Track the chain visually: the Breadcrumbs panel on each phase note shows
previous/next phase edges derived from the `prev:` and `next:` frontmatter
fields. See the Kanban view at [[11-migration/migration-board]].

### Static phase list

Eight phases, each self-contained. Each has a clear "done" signal, a rollback plan, and a quick smoke test.

- [[11-migration/phase-1-schema]] — Introduce pydantic schemas; dual-write to the existing collection.
- [[11-migration/phase-2-hybrid-search]] — Add sparse + named vectors; keep old collection.
- [[11-migration/phase-3-reranker]] — TEI reranker service; deep path.
- [[11-migration/phase-4-planes]] — Split `musubi` into `episodic`, `curated`, `concept`, `artifact_chunks`, `thoughts`.
- [[11-migration/phase-5-vault]] — Obsidian vault + watcher + write-log.
- [[11-migration/phase-6-lifecycle]] — APScheduler + lifecycle events; maturation, synthesis, promotion.
- [[11-migration/phase-7-adapters]] — Break out MCP into its own adapter; add LiveKit + OpenClaw adapters.
- [[11-migration/phase-8-ops]] — Kong, snapshots, observability, Ansible.
- [[11-migration/re-embedding]] — Re-embedding strategy when a model version changes.
- [[11-migration/schema-evolution]] — How pydantic + Qdrant payloads evolve safely.
- [[11-migration/scaling]] — Beyond one box: multi-host, HA, sharding.

## Principles

1. **Additive first, destructive last.** Every phase adds capability without removing the old. Removal happens after the new is proven.
2. **Forward compatibility preserves POC tokens.** Existing Claude Code / CLI sessions keep working through every phase. If they break, the phase isn't done.
3. **Reversibility.** Every phase has a rollback. We run rollback drills before merge.
4. **Dual-write when possible.** Between old-path and new-path; lets us compare outputs live.
5. **Shadow-eval.** Before flipping retrieval, run both old and new in parallel on real queries; compare results.
6. **Write from the vault is the last change.** Until phase 5, the vault is read-only (humans edit). After phase 5, lifecycle worker also writes — but through the write-log to prevent echo.

## Order matters

Phase dependencies:

```
1 ──▶ 2 ──▶ 3
│     │
│     ▼
│     4 ──▶ 5 ──▶ 6
│                 │
└─────────────────▼
                  7 ──▶ 8
```

- Schema (1) unblocks everything.
- Hybrid (2) and reranker (3) are independent of planes.
- Planes (4) must be in place before the vault (5) because curated goes into a named collection.
- Vault (5) must be in place before lifecycle (6) because promotion writes to vault.
- Adapters (7) come after lifecycle so they can rely on stable behavior.
- Ops (8) polishes; can start anytime but finishes last.

## Duration estimate

Rough, with one developer part-time:

| Phase | Effort |
|---|---|
| 1. Schema | ~1 week |
| 2. Hybrid search | ~2 weeks |
| 3. Reranker | ~3 days |
| 4. Planes | ~2 weeks |
| 5. Vault | ~2 weeks |
| 6. Lifecycle | ~3 weeks |
| 7. Adapters | ~3 weeks |
| 8. Ops | ~2 weeks |

Total: ~3 months calendar. Order can overlap where dependencies allow.

## What we're NOT doing

- Not moving off Qdrant (it's the right tool).
- Not adding a knowledge graph (see [[13-decisions/0004-no-knowledge-graph-v1]]).
- Not building a web UI (vault + dashboards + CLI are the UI).
- Not switching to a hosted API (local inference is the thesis).
- Not multi-tenant at the host level (one household, one box).

## Cross-cutting concerns

- Re-embedding: [[11-migration/re-embedding]].
- Schema versioning: [[11-migration/schema-evolution]].
- Scaling: [[11-migration/scaling]].
