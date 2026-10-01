---
title: Operations
section: 09-operations
tags: [index, operations, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Operations

Day-two concerns: backup, monitoring, incident response, and the asset matrix that says
what's canonical and what's derived.

Everything here assumes the single-host Compose stack ([[08-deployment/index]]). The
user-facing summary is in `docs/guide/operate.md`.

## Docs in this section

- [[09-operations/asset-matrix]] — Canonical vs derived stores. Source of truth for each piece of data.
- [[09-operations/backup-restore]] — The cold backup set, Qdrant snapshots, restore procedures.
- [[09-operations/observability]] — Metrics, logs, traces Musubi actually emits.
- [[09-operations/alerts]] — Recommended alert rules and thresholds (none are shipped).
- [[09-operations/runbooks]] — Step-by-step procedures for common incidents.
- [[09-operations/capacity]] — Capacity planning, thresholds, scale signals.

## Principles

1. **The vault is the source of truth for curated knowledge.** Qdrant's curated
   collection is derived from it.
2. **Artifact blobs are the source of truth for their content.** Chunks and embeddings are
   derived from them.
3. **Episodic memories, concepts and thoughts live only in Qdrant.** No second copy. If
   Qdrant is lost without a backup, they are lost. Backups are therefore non-negotiable.
4. **Thoughts are not kept forever by design, but nothing deletes them today.**
   `src/musubi/ops/retention.py` defines a 30-day hard delete for thoughts, but no process
   in the public stack runs it.
5. **Every backup must round-trip.** Untested backups are not backups. Test a restore at
   least every 90 days.
6. **Operators act through the API.** Don't hand-edit Qdrant payloads, vault frontmatter or
   sqlite tables under load. Lifecycle changes go through the operator endpoints
   ([[07-interfaces/canonical-api]]).

## The asset matrix at a glance

| Asset | Canonical | Derived / Index | Backup | Recovery |
|---|---|---|---|---|
| Episodic memories | Qdrant | — | cold volume backup | Restore the set |
| Curated docs (content + frontmatter) | Vault (Markdown) | Qdrant | cold volume backup (+ optional git) | Restore the set |
| Concepts | Qdrant | — | cold volume backup | Restore the set |
| Artifacts (blob) | `artifact-blobs` volume | — | cold volume backup | Restore the set |
| Artifacts (metadata + chunks) | Qdrant | — (no re-chunk tool exists) | cold volume backup | Restore the set |
| Thoughts | Qdrant | — | cold volume backup | Restore the set |
| Lifecycle state (event log, cursors, outbox) | sqlite in the `lifecycle` volume | — | cold volume backup | Restore the set |
| Config | `docker-compose.yml` + your `.env` | — | your own config management | Re-apply |

Full detail: [[09-operations/asset-matrix]].

## On-call burden

v1 targets a **very low** on-call burden:

- Single host, no distributed coordination.
- No paging for synthesis or reflection failures; they run again on their next schedule.
- [[09-operations/alerts]] recommends paging only on: Core down, Core 5xx above 1% for
  5 minutes, a dependency down, the lifecycle worker not ready, disk nearly full, and a
  missed backup.
