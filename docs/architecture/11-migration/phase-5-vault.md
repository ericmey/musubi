---
title: "Phase 5: Vault"
section: 11-migration
tags: [migration, obsidian, phase-5, section/migration, status/stub, type/migration-phase, vault]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-4-planes]]"
next: "[[11-migration/phase-6-lifecycle]]"
reviewed: false
---
# Phase 5: Vault

Make the Obsidian vault the source of truth for curated knowledge. Add the vault watcher + write-log.

## Goal

Humans write in Obsidian. Watcher picks up changes, indexes them into `musubi_curated`. Core writes (from promotions in phase 6) go through a write-log so the watcher ignores its own echoes.

## Changes

### Vault directory

```
/var/lib/musubi/vault/
  concepts/         # (phase 6 writes here)
  runbooks/
  projects/
  notes/
  reflections/
  .obsidian/
```

Ansible creates the dirs. Git init with remote push cron.

### Frontmatter schema

See [[06-ingestion/vault-frontmatter-schema]]. Enforcement: if a file has invalid frontmatter, watcher logs a warning + skips indexing until fixed. User sees a validation error in the "vault events" dashboard.

### Watcher

Python watchdog observing `/var/lib/musubi/vault/**/*.md`. See [[06-ingestion/vault-sync]]. Key features:

- 2s debounce.
- Event handlers for created/modified/moved/deleted.
- Write-log lookup to filter echoes.
- Boot scan reconciles on startup (catches edits made while offline).

### Write-log table

```sql
CREATE TABLE write_log (
  vault_path TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  written_by TEXT NOT NULL,   -- e.g., 'system:promotion'
  written_at INTEGER NOT NULL,
  consumed_at INTEGER
);
```

When the lifecycle engine writes a new curated file, it inserts a row. Watcher consults before indexing; if the `content_hash` matches and `consumed_at` is null, it marks consumed and doesn't re-index (Core already updated Qdrant directly when it wrote).

### Initial import

Start empty (no legacy curated docs). User seeds manually or via future promotion.

### Retrieve integration

`musubi_curated` participates in retrieval. Blended results include curated hits. In phase 6, concepts are added; until then, only episodic + curated are available in blended.

## Done signal

- Writing a new `.md` in the vault causes a `musubi_curated` upsert within 5s.
- Moving / renaming preserves object_id (tracked via frontmatter `object_id`).
- Deletion archives the curated row (`state=archived`).
- Boot scan reconciles edits made while offline.
- Echo test: fake a write via `sqlite3` insert + file touch; watcher must see it was consumed.

## Rollback

Phase 5 is additive. To roll back: stop the watcher, drop `musubi_curated`, keep the vault directory. Nothing about episodic / thoughts is affected.

## Smoke test

```
# User edits a file in the vault.
echo '---\ntitle: Test\nobject_id: test-01\n---\ncontent' > vault/notes/test.md
# Watcher should upsert into musubi_curated within a few seconds.
sleep 5
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8100/v1/curated-knowledge/test-01 | jq .title
# -> "Test"
```

## Estimate

~2 weeks.

## Pitfalls

- **Debounce shorter than edit cadence** → stuck updates. 2s is conservative.
- **Cross-partition moves** → watchdog sometimes reports move as separate delete+create. Handle both cases; reconcile via `object_id`.
- **Empty .md files** from Obsidian's live-preview pre-save. Watcher ignores until frontmatter is present.
- **`.obsidian/` churn**. Watcher path filter excludes dotfolders.
- **Binary files**. Watcher filters `.md` only. Non-Markdown files are out of scope (use artifact upload).
