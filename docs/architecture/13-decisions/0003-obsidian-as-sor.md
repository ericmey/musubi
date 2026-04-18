---
title: "ADR 0003: Obsidian Vault as Source of Record for Curated Knowledge"
section: 13-decisions
tags: [adr, curated, obsidian, section/decisions, status/accepted, type/adr, vault]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0003: Obsidian Vault as Source of Record for Curated Knowledge

**Status:** accepted
**Date:** 2026-03-14
**Deciders:** Eric

## Context

Curated knowledge — the plane intended for polished, human-trusted information — needs a home that is:

1. Human-writable. The person should be able to edit it directly, ideally in a tool they already use.
2. Durable. Outlives the vector DB. Survives a Qdrant rebuild.
3. Version-controlled. Every change is diffable; easy rollback.
4. Portable. Plain files, not locked into a vendor format.
5. Offline-capable. Edit on the plane, sync later.

Options considered:

- **Qdrant only.** Curated lives as vectors+payload in a Qdrant collection. Users edit via API. Loses durability guarantees (Qdrant is derived state); loses human-editability (who writes in Qdrant?).
- **Postgres + markdown blobs.** Central DB with a `curated_docs` table. Breaks offline editing; requires a sync protocol anyway.
- **Google Docs / Notion / other SaaS.** Vendor lock-in; no git; dubious for household-sensitive content.
- **Obsidian vault.** Plain-markdown files on disk, syncable via git (or Syncthing/Obsidian Sync), human-editable in the user's existing tool.

Eric already uses Obsidian daily. The vault already exists. The cost of adopting it as a structural decision is ~zero.

## Decision

The Obsidian vault is the **source of record (SoR) for curated knowledge**. Qdrant holds a *derived index* of the vault, not the vault's content.

Rules:

- If the vault disagrees with Qdrant, the **vault wins**. Qdrant gets re-indexed.
- A vault-sync job watches the vault directory and mirrors every file to Qdrant ([[06-ingestion/vault-sync]]).
- Concept-to-curated promotion writes a new file to the vault first, then Qdrant picks it up via the watcher.
- The vault can be rebuilt from git at any time; Qdrant can be rebuilt from the vault at any time.

## Alternatives

See Context. The key rejection: making Qdrant the writer of curated content. Curated is the plane where human trust lives; Qdrant is inherently derived/approximate.

## Consequences

- Vault watcher is a first-class component, not an afterthought ([[04-data-model/vault-schema]], [[06-ingestion/vault-sync]]).
- Echo-prevention write-log becomes necessary ([[11-migration/phase-5-vault]]): when the system writes a vault file, the resulting filesystem event must not be re-indexed as if a human wrote it.
- Vault layout becomes part of the public API — users' filepaths matter for retrieval scoping.
- Backup strategy: vault gets git pushes (frequent, cheap); Qdrant gets snapshots (less frequent, rebuildable).
- Deploy story: moving vault to a new host is `git clone`. Qdrant can follow at its own pace.

Trade-offs:

- Two systems to keep in sync (vault ↔ Qdrant). We accept the complexity because the durability + human-edit-ability wins are large.
- Obsidian-specific plugins (Dataview, Templater) are not the SoR — plain markdown is. If plugins add front-matter, that's fine; if they rely on dynamic queries, those queries live client-side only.

## Links

- [[13-decisions/0001-three-plane-architecture]]
- [[04-data-model/vault-schema]]
- [[06-ingestion/vault-sync]]
- [[11-migration/phase-5-vault]]
