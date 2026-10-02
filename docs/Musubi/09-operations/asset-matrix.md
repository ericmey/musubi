---
title: Asset Matrix
section: 09-operations
tags: [backup, canonical, derived, operations, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: false
---
# Asset Matrix

Where each piece of data lives, who writes it, and what happens when the store is lost.

**Principle:** if we lose a derived store, we rebuild it. If we lose a canonical store
without a backup, the data is gone. Keep the canonical set small and well backed up.

All state lives in the Compose stack's named volumes: `qdrant-storage`,
`qdrant-snapshots`, `vault`, `artifact-blobs`, `lifecycle` and `logs`. The public backup
procedure archives all six cold, as one set ([[09-operations/backup-restore]]), so the
recovery point for every row below is the time of your last complete backup set.

## The matrix

| Data | Canonical store | Derived stores | Backup | Notes |
|---|---|---|---|---|
| Episodic memory | Qdrant `musubi_episodic` (`qdrant-storage`) | — | cold volume set | only copy |
| Curated knowledge (body + frontmatter) | Markdown in the `vault` volume | Qdrant `musubi_curated` | cold volume set; optionally also git | the worker's 6-hourly `vault_reconcile` upserts vault files into Qdrant |
| Synthesized concept | Qdrant `musubi_concept` | — | cold volume set | only copy |
| Artifact (blob bytes) | `artifact-blobs` volume | — | cold volume set | stored as `<namespace>/<object_id>`; its SHA-256 is in the artifact metadata |
| Artifact (metadata) | Qdrant `musubi_artifact` | — | cold volume set | title, tags, source |
| Artifact chunks (text + vectors) | Qdrant `musubi_artifact_chunks` | derivable from blobs in principle | cold volume set | no re-chunk tool exists |
| Thoughts | Qdrant `musubi_thought` | — | cold volume set | only copy |
| Lifecycle audit mirror | Qdrant `musubi_lifecycle_events` | — | cold volume set | |
| Lifecycle state (event log, cursors, synthesis candidates, outbox) | sqlite `work.sqlite` in the `lifecycle` volume | — | cold volume set | |
| Idempotency receipts | sqlite `idempotency-receipts.sqlite` in the `lifecycle` volume | — | cold volume set | |
| Vault write-log (echo prevention) | sqlite `vault-writelog.db` in the `lifecycle` volume | — | cold volume set | |
| Qdrant snapshots | `qdrant-snapshots` volume | — | cold volume set | only if you take them |
| Config | root `docker-compose.yml` + your `.env` | — | your own config management | keep `.env` private |
| Secrets (`JWT_SIGNING_KEY`, `QDRANT_API_KEY`, …) | your secret manager or private `.env` | — | your secret manager, off-host | losing `JWT_SIGNING_KEY` invalidates every token |
| Issued tokens | not stored; JWTs signed with `JWT_SIGNING_KEY` | — | re-mint from the signing key | |
| Model caches (GPU overlay) | `tei-models`, `ollama-models` volumes | — | none needed | re-download on start |

## Canonical store ownership

### Vault (`vault` volume)

**Owns:** curated Markdown body text and frontmatter, including files the lifecycle
worker writes on promotion and reflection.

**Does not own:** episodic memories or vectors (Qdrant only).

**Write access:** humans editing the vault, and the lifecycle worker (promotion,
reflection). Core and the worker both mount the volume.

**Backup:** part of the cold set. Keeping the vault in git and pushing to a private remote
on a schedule gives curated knowledge its own history; Musubi does not ship a script for
that.

### Artifact blobs (`artifact-blobs` volume)

**Owns:** raw file bytes for uploaded artifacts, stored at `<namespace>/<object_id>`. The
artifact's metadata row records the blob's SHA-256.

**Write access:** Core's artifact upload. Read-only after write.

### Qdrant (`qdrant-storage` volume)

**Owns:** episodic memories, concepts, thoughts, artifact metadata and chunks, the
lifecycle audit mirror, and a **copy** of curated knowledge.

**Write access:** Core and the lifecycle worker.

### sqlite (`lifecycle` volume)

**Owns:** `work.sqlite` (lifecycle event log, maturation and synthesis cursors, synthesis
candidates, lifecycle outbox), `idempotency-receipts.sqlite`, `vault-writelog.db`, and
job lock files under `locks/`.

**Write access:** Core (idempotency receipts, lifecycle store) and the lifecycle worker.

## Derivability

### Curated in Qdrant

The lifecycle worker's `vault_reconcile` job (every 6 hours) walks the vault and upserts
every Markdown file that carries an `object_id` into the curated plane. There is no
separate rebuild command.

### Artifact chunks in Qdrant

Derivable from the blobs in principle, but no re-chunk command exists (planned, not
implemented). Restore chunks from the backup set.

### Artifact metadata in Qdrant

Not derivable: it carries user-supplied title, tags and topics. Restore from the backup
set.

## Data that is not canonical anywhere else

Episodic memories, concepts, thoughts and artifact metadata live only in Qdrant. If both
Qdrant and every backup set are lost, they are gone. That is why backups go off the host.

## Retention policies

| Data | Retention |
|---|---|
| Episodic memory (provisional) | archived after 7 days by the hourly `provisional_ttl` job (archived, not deleted) |
| Episodic memory (matured) | until demoted; see [[06-ingestion/demotion]] |
| Curated | until deleted by hand |
| Concept | until rejected or promoted |
| Thoughts | kept: a 30-day hard delete exists in `src/musubi/ops/retention.py`, but nothing in the public stack runs it |
| Artifacts | kept; opt-in archival after 180 days unreferenced with `MUSUBI_ARTIFACT_ARCHIVAL_ENABLED=true` (blob bytes kept) |
| Lifecycle outbox (terminal rows) | 30 days (`lifecycle_cleanup_retention_s`), pruned by the worker |
| Backup sets and snapshots | your choice |
| Container logs | your Docker logging driver's settings |

## Ownership boundaries

Rule: **one writer per canonical row.**

| Row | Writer |
|---|---|
| Episodic row | Capture API (create); lifecycle transitions (state changes) |
| Curated row | Curated API, vault reconcile from vault edits, or lifecycle promotion |
| Concept row | Synthesis job (create); lifecycle transitions (state changes) |
| Artifact metadata | Artifact upload API (create); operator lifecycle actions |
| Artifact chunk | The lifecycle worker's artifact indexer, after upload |

If two writers could touch a row, one gets authority (operator precedence) or the row is
versioned and conflicts are detected; never a silent overwrite.
