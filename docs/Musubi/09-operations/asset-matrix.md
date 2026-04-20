---
title: Asset Matrix
section: 09-operations
tags: [backup, canonical, derived, operations, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: false
---
# Asset Matrix

Where each piece of data lives, who owns it, and what happens when the store is lost.

**Principle:** if we lose a derived store, we rebuild it. If we lose a canonical store without a backup, data is gone. Keep the canonical set small and well-backed-up.

## The matrix

| Data | Canonical store | Derived stores | Backup strategy | RPO target |
|---|---|---|---|---|
| Episodic memory | Qdrant `musubi_episodic` | — | Qdrant snapshot (6h) → SATA SSD | 6 hours |
| Curated knowledge (body) | Vault `.md` files | Qdrant `musubi_curated` | git push (15min) → remote repo | 15 minutes |
| Curated knowledge (frontmatter) | Vault `.md` frontmatter | Qdrant payload | git push (15min) | 15 minutes |
| Synthesized concept | Qdrant `musubi_concept` | — | Qdrant snapshot (6h) | 6 hours |
| Artifact (blob) | `/var/lib/musubi/artifact-blobs/` | — | rsync to SATA SSD (hourly) | 1 hour |
| Artifact (metadata) | Qdrant (artifact head row) | — | Qdrant snapshot (6h) | 6 hours |
| Artifact chunks (text + vector) | Qdrant `musubi_artifact_chunks` | (regenerable from blob) | Qdrant snapshot (6h) | 6 hours (faster via re-chunk) |
| Thoughts | Qdrant `musubi_thoughts` | — | Qdrant snapshot (6h) | 6 hours |
| Lifecycle events | sqlite `lifecycle-work.sqlite:lifecycle_events` | — | sqlite `.backup` (daily) → SATA | 24 hours |
| Write-log (vault ↔ Qdrant echo) | sqlite `lifecycle-work.sqlite:write_log` | — | sqlite `.backup` (daily) | (can regenerate partially) |
| Schedule locks | sqlite `lifecycle-work.sqlite:schedule_locks` | — | sqlite `.backup` (daily) | (stateless; fine to lose) |
| Config | `/etc/musubi/` + `.env` | — | Ansible git repo + `.vault.yml` | Git push (per change) |
| Secrets | 1Password + `.vault.yml` | — | 1Password | Real-time |
| OAuth tokens (issued) | JWT signed, not persisted | — | Re-issue from signing key | — |
| Qdrant config | `/etc/musubi/qdrant-config.yaml` | — | Ansible git | Per-change |

## Canonical store ownership

### Vault (`/var/lib/musubi/vault/`)

**Owns:**

- Curated Markdown body text.
- Curated frontmatter.
- `reflections/` (daily AI-generated reflections).
- `.obsidian/` config (kept per-user; don't sync Obsidian plugin state from agent-side writes).

**Does not own:**

- Episodic memories (those live in Qdrant only).
- Vector embeddings (stored in Qdrant).

**Write access:**

- Human via Obsidian editor.
- Lifecycle Worker (concept promotion writes new curated files).
- Nothing else.

**Backup:** git push every 15 min (cron on host). Remote repo on GitHub private. See [[09-operations/backup-restore#vault]].

### Artifact blobs (`/var/lib/musubi/artifact-blobs/`)

**Owns:** raw file bytes for uploaded artifacts (PDF, HTML, VTT, etc.), content-addressed.

**Write access:** Musubi Core's artifact service on upload. Read-only after write.

**Backup:** hourly rsync to `/mnt/snapshots/artifact-blobs/`. 90-day retention on the SATA SSD.

### Qdrant (`/var/lib/musubi/qdrant/`)

**Owns:**

- Episodic memories (body + vector + payload).
- Synthesized concepts.
- Thoughts.
- Artifact head rows + artifact chunks (text + vectors).
- **Copy** of curated (derived from vault on sync).

**Write access:** Musubi Core.

**Backup:** full snapshot every 6 hours (via Qdrant snapshot API) → rsync to SATA.

### sqlite (`/var/lib/musubi/lifecycle-work.sqlite`)

**Owns:**

- Lifecycle events log (append-only).
- Write-log for vault ↔ Qdrant echo prevention.
- Schedule locks for APScheduler.
- Boot-scan cursors.
- Concept synthesis watermarks.

**Write access:** Lifecycle Worker, Vault Watcher.

**Backup:** daily `sqlite3 lifecycle-work.sqlite .backup /mnt/snapshots/lifecycle.sqlite.<ts>`. Point-in-time restore via replay of `lifecycle_events`.

## Derivability

Each derived store can be rebuilt from its canonical source:

### Curated in Qdrant

Loss recovery:

1. Stop Core.
2. `DELETE collection musubi_curated`.
3. `musubi-cli index rebuild --collection musubi_curated --source vault`.
4. Start Core.

Rebuild time at v1 scale: a few minutes per 1000 docs (dense + sparse encode on GPU).

### Artifact chunks in Qdrant

Loss recovery:

1. `musubi-cli artifacts rechunk --all`.
2. Worker pulls each blob, re-runs the chunker, reinserts chunks.

Rebuild time: depends on chunker. HTML/PDF ~5-20 min per 1000 artifacts.

### Artifact head rows in Qdrant

Less trivially regenerable — the head row carries user-supplied metadata (title, tags, topics). If only Qdrant is lost but snapshots exist, restore from snapshot. If snapshots are also lost, reconstruct from `artifact-blobs/` directory listing + file metadata — title falls back to filename, tags are empty. Not great; that's why snapshots are non-negotiable.

## Data that isn't truly canonical anywhere else

Two classes of data live only in Qdrant + snapshots:

1. **Episodic memories.**
2. **Concepts.**

For these, Qdrant snapshots are the only backup. If all of {Qdrant, snapshots on NVMe, snapshots on SATA} are lost, the data is gone. This is why snapshots go to the SATA SSD (separate disk), and daily ones rotate off-host to the NAS/cloud if configured.

Artifact **content** (blob) is redundant with the rsync copy — losing Qdrant doesn't lose the PDF. Artifact **chunks+vectors** are rebuildable from the blob. Artifact **head row** is the one piece that's only in Qdrant+snapshot.

## Retention policies

| Data | Retention |
|---|---|
| Episodic memory (matured) | Indefinite until demoted (see [[06-ingestion/demotion]]) |
| Episodic memory (provisional, unenriched) | 7 days |
| Curated | Indefinite until manual delete |
| Concept | Until rejected or promoted |
| Thoughts (read) | 90 days then soft-delete |
| Thoughts (unread) | 180 days then soft-delete |
| Artifacts | Indefinite; hard-delete via operator |
| Lifecycle events | 180 days (configurable) |
| Write-log | 30 days after `consumed_at` |
| Snapshots | 90 days rolling |
| Access logs | 30 days |

## Ownership boundaries

Rule: **a single writer per canonical row.**

| Row | Single writer |
|---|---|
| Episodic row | Capture API (or Lifecycle Worker for state transitions) |
| Curated frontmatter | Vault Watcher ingesting vault edits, or Lifecycle Worker on promotion |
| Curated body | Only vault write + Watcher echo |
| Concept row | Concept Synthesis job (create) / Lifecycle Worker (state transitions) |
| Artifact head | Artifact upload API (create) / operator (archive/purge) |
| Artifact chunk | Chunker (create) / Lifecycle (archive) |

If two writers could touch a row, we either give one authority (operator precedence) or version it and detect conflicts — never silently overwrite.

## Test contract

**Module under test:** no specific code — this doc is a contract for the rest.

1. `test_every_asset_has_canonical_owner_documented` (doc lint)
2. `test_backup_cadence_matches_claimed_rpo`
3. `test_restore_drills_run_quarterly` (operational; see [[09-operations/runbooks]])
4. `test_curated_rebuild_from_vault_produces_matching_qdrant_count`
5. `test_artifact_rechunk_produces_same_chunk_count_as_snapshot`
