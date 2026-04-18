---
title: Operations
section: 09-operations
tags: [index, operations, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Operations

Day-two concerns: backup, monitoring, incident response, the asset matrix that tells us what's canonical vs. derived.

Everything here assumes single-host v1 ([[08-deployment/index]]). Multi-host ops is in [[11-migration/scaling]].

## Docs in this section

- [[09-operations/asset-matrix]] — Canonical vs derived stores. What's source of truth for each piece of data.
- [[09-operations/backup-restore]] — Backup strategy, snapshot cadence, restore procedures.
- [[09-operations/observability]] — Metrics, logs, traces. What to look at, when.
- [[09-operations/alerts]] — Alert rules, thresholds, on-call actions.
- [[09-operations/runbooks]] — Step-by-step procedures for common incidents.
- [[09-operations/capacity]] — Capacity planning, thresholds, scale signals.

## Principles

1. **Obsidian vault is source of truth for curated knowledge.** Everything else is derived — losing Qdrant is inconvenient (rebuild from vault + artifact blobs); losing the vault is catastrophic.
2. **Artifact blobs are source of truth for their content.** Chunks and embeddings are derived; can be regenerated.
3. **Episodic memories live only in Qdrant + snapshots.** No second copy. If Qdrant is lost without a snapshot, episodic history is lost. Snapshots are therefore non-negotiable.
4. **Thoughts are ephemeral-ish.** Stored in Qdrant; backed up with episodic. Older-than-90-day thoughts soft-delete by default.
5. **Every backup must round-trip.** Untested backups are not backups. Restore drills run quarterly.
6. **Operators act through the API.** Don't hand-edit Qdrant payloads, vault frontmatter, or sqlite tables under load. Operator endpoints exist for a reason ([[07-interfaces/canonical-api#lifecycle]]).

## The asset matrix at a glance

| Asset | Canonical | Derived / Index | Backup | Recovery |
|---|---|---|---|---|
| Episodic memories | Qdrant | — | Qdrant snapshot | Restore snapshot |
| Curated docs (content) | Vault (Markdown) | Qdrant | Git | Clone git + reindex |
| Curated docs (metadata) | Vault frontmatter | Qdrant | Git | Clone git + reindex |
| Concepts | Qdrant | — | Qdrant snapshot | Restore snapshot |
| Artifacts (blob) | artifact-blobs/ | — | rsync to SATA SSD | Copy back |
| Artifacts (metadata) | Qdrant | — | Qdrant snapshot | — |
| Artifact chunks | Qdrant (text+vector) | (rebuildable from blob) | Qdrant snapshot | Restore or re-chunk |
| Thoughts | Qdrant | — | Qdrant snapshot | — |
| Lifecycle events | sqlite (`lifecycle-work.sqlite`) | — | sqlite backup cron | Restore file |
| Write-log (vault echo prevention) | sqlite | — | sqlite backup cron | — |
| Config | `/etc/musubi/` | — | Ansible repo | Re-run playbook |

Full detail: [[09-operations/asset-matrix]].

## On-call burden

v1 targets a **very low** on-call burden:

- Single host, no distributed coordination.
- No paging for synthesis / reflection failures (they retry tomorrow).
- Alerts fire only on: vault fs full, Qdrant down, Core 5xx > 1% for 5min, GPU OOM, backup failure > 24h.

See [[09-operations/alerts]] for the full policy.
