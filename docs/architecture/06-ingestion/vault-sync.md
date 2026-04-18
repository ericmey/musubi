---
title: Vault Sync
section: 06-ingestion
tags: [ingestion, obsidian, section/ingestion, status/draft, type/spec, vault, watcher]
type: spec
status: draft
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Vault Sync

Syncing the Obsidian vault (source of record for curated knowledge) to Qdrant (derived index). Bidirectional flow: human edits propagate to the index; promotion writes propagate to the index; neither re-triggers the other.

See [[04-data-model/vault-schema]] for the schema details and [[13-decisions/0003-obsidian-as-sor]] for the rationale.

## The component

`musubi-vault-watcher` is a dedicated process (not a thread in Core). Why separate:

- **Isolation**: if Watcher OOMs or crashes, it doesn't take Core with it.
- **Restart safety**: Watcher on boot does a full scan; slow on large vaults. Better not to block Core's startup.
- **Resource profile**: Watcher is I/O bound; Core is CPU/GPU-bound. Separate processes let us size them independently.

## Technologies

- **watchdog** (Python) for filesystem events — cross-platform, reliable on ext4 + APFS.
- **inotify** directly when watchdog's polling fallback would be needed (we're on Linux; inotify is fine).
- **pydantic** for frontmatter validation.
- **ruamel.yaml** for round-tripping YAML (preserves formatting, unlike PyYAML).

## File event handling

Events we care about:

- `on_created(path)`
- `on_modified(path)`
- `on_moved(src, dest)`
- `on_deleted(path)`

Events we ignore:

- `.obsidian/*`, `.git/*`, any dotfile or `_`-prefixed directory.
- Non-`.md` files in `curated/` (we only index markdown).
- Temporary files: `*.tmp`, `*~`, `.*.swp`.

## Debouncing

Obsidian often writes a file multiple times in quick succession (autosave + manual save). We debounce per-path: after an event, wait 2s (`WATCHER_DEBOUNCE_SEC`). If the same path fires again during that window, extend. On quiet, process.

Debounce state lives in memory. On Watcher restart, all pending events are lost — the boot-time scan will catch them.

## Handling each event

### on_created / on_modified

```
1. Check write-log — did we (Core) just write this ourselves?
   → if yes and body_hash matches: consume, return.
2. Read file bytes.
3. Parse YAML frontmatter.
   → on parse error: log, emit Thought to ops-alerts, return.
4. Validate frontmatter (pydantic CuratedFrontmatter).
   → on validation error: emit Thought with details, return.
5. Compute body_hash (sha256 of body bytes).
6. If file has no object_id: generate KSUID, write frontmatter back.
   → this is a write; add write-log entry (written_by=core), but keep
     musubi-managed: false (we're bootstrapping an ID, not taking over).
7. Look up existing Qdrant point by (namespace, object_id).
   → if no existing point, or body_hash changed: re-embed + upsert.
   → else: no-op (frontmatter-only change, maybe just a wikilink add — re-index metadata only).
8. If state frontmatter field changed: emit LifecycleEvent via transition().
9. Update `vault_path` in the Qdrant point if it differs.
```

### on_moved (rename)

```
1. Check write-log.
2. Read file, parse frontmatter, confirm object_id.
3. Update `vault_path` in Qdrant — no re-embedding needed.
4. Log the move.
```

### on_deleted

```
1. Check write-log.
2. Look up Qdrant point by (namespace, object_id-from-last-known-state).
   → The on-disk file is gone; we remember the mapping via Watcher's sqlite state.
3. Transition state to 'archived'.
4. Move the file to _archive/YYYY-MM-DD/ — but wait, the file is already gone.
   → This means we missed the move-to-archive (rare). Log a warning.
```

Normal delete flow: **user deletes in Obsidian** → on_deleted fires → we archive. But the **recommended flow** is to use `state: archived` in frontmatter OR move to `_archive/` manually; that way the file is preserved.

A delete-vs-archive preference is stored in `config.VAULT_DELETE_BEHAVIOR`:

- `"archive-only"` (default): on_deleted transitions to `archived` but *does not* re-create the file. A hard delete happened. Emit an ops-alerts Thought for safety.
- `"refuse"`: on_deleted triggers a `VaultRestore` — rehydrate the file from git HEAD. Aggressive; for paranoid setups.

## Echo prevention (detail)

The write-log is a sqlite DB shared by Core and Watcher:

```
/srv/musubi/vault-state/write-log.sqlite
```

Schema:

```sql
CREATE TABLE writes (
  file_path TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  written_by TEXT NOT NULL,
  written_at REAL NOT NULL,
  consumed_at REAL DEFAULT NULL,
  PRIMARY KEY (file_path, body_hash)
);
CREATE INDEX idx_written_at ON writes(written_at);
```

Flow:

```
Core promotion → writes row (written_by='core', consumed_at=NULL)
              → writes file

Watcher sees event → checks log for (path, body_hash)
                  → if row exists, written_by='core', consumed_at=NULL:
                    → set consumed_at=now; ignore event.
                  → else: process the event.

Cleanup: rows older than 5m with consumed_at=NULL → warning ("orphaned Core write").
         rows older than 1h → purged.
```

## Boot-time scan

On Watcher startup:

1. Read all `.md` files under `curated/` (and `reflections/` if enabled).
2. For each file: parse frontmatter, compute body_hash.
3. Compare against last-indexed state (stored in Watcher's sqlite `/srv/musubi/vault-state/index-state.sqlite`).
4. Diff:
   - New files: index.
   - Modified (body_hash or frontmatter meta changed): re-index.
   - Removed: archive (if `VAULT_DELETE_BEHAVIOR != "refuse"`).

For 10K curated files, expected boot scan time is ~60s (most of which is pydantic validation + hashing, not I/O).

## Large file handling

Files > 2KB or with > 4 H2 sections are chunked into artifact chunks:

1. The `CuratedKnowledge` point embeds `title + summary + first 2K of content`.
2. Additional content is stored as `ArtifactChunk` rows in `musubi_artifact_chunks` under a synthetic artifact (`source_system: "vault-curated"`, `derived_from: <curated_id>`).
3. Retrieval considers both — a chunk hit resolves back to the parent curated via `derived_from`.

The threshold is configurable (`VAULT_LARGE_FILE_THRESHOLD_BYTES`).

## Reconciler

Runs every 6 hours, checks for drift between vault and Qdrant:

```
for each curated_point in musubi_curated:
    if not fs.exists(curated_point.vault_path):
        → orphan index: archive it.
for each file in vault/curated/**/*.md:
    if file.object_id not in musubi_curated:
        → orphan file: re-index it.
    elif point.body_hash != file.body_hash:
        → drift: re-index.
```

Reconciler is idempotent; running twice back-to-back is a no-op on the second run.

See [[09-operations/asset-matrix]] for the canonical vs derived catalog.

## Rate limits

A malicious or broken editor could thrash the vault:

- Max events per second: 100 (drops with a warning beyond that).
- Max indexing writes per minute: 1000 (batched; drops with alert beyond).

These protect Qdrant and TEI from a pathological human who runs a shell script on the vault.

## Test contract

**Module under test:** `musubi/vault/watcher.py`, `musubi/vault/writer.py`, `musubi/vault/reconciler.py`

Events:

1. `test_on_created_indexes_new_file`
2. `test_on_modified_reindexes_body_change`
3. `test_on_modified_frontmatter_only_no_reembed`
4. `test_on_moved_updates_vault_path`
5. `test_on_deleted_archives_point`
6. `test_dotfile_ignored`
7. `test_underscore_dir_ignored`

Debounce:

8. `test_debounce_multiple_rapid_writes_process_once`
9. `test_debounce_extends_on_new_event_during_window`

Validation:

10. `test_invalid_yaml_emits_thought_and_skips`
11. `test_missing_required_field_emits_thought`
12. `test_body_only_no_frontmatter_rejected`
13. `test_missing_object_id_gets_generated_and_written_back`

Echo prevention:

14. `test_writelog_matches_core_write_event_consumed`
15. `test_writelog_mismatch_body_hash_reindexes`
16. `test_writelog_orphan_older_than_5m_logged_as_warning`
17. `test_writelog_entry_purged_after_1h`

Boot scan:

18. `test_boot_scan_indexes_new_files`
19. `test_boot_scan_detects_body_hash_change`
20. `test_boot_scan_archives_removed_files`

Large files:

21. `test_large_file_body_chunked_as_artifact`
22. `test_large_file_curated_embeds_summary`

Reconciler:

23. `test_reconciler_detects_orphan_point`
24. `test_reconciler_detects_orphan_file`
25. `test_reconciler_reindexes_drifted_body_hash`
26. `test_reconciler_idempotent_on_second_run`

Rate limits:

27. `test_event_rate_limit_drops_with_warning`
28. `test_indexing_rate_limit_backpressure`

Property:

29. `hypothesis: for any sequence of file-system events, Watcher + Reconciler converge to a state where vault ≡ Qdrant`

Integration:

30. `integration: human-edit-round-trip — save .md file, watcher indexes, retrieval returns it`
31. `integration: Core-promotion-round-trip — Core writes file, watcher ignores via write-log, point correct`
32. `integration: reconciler recovery — delete a Qdrant point behind Watcher's back, reconciler re-indexes from file`
33. `integration: 10K file boot scan completes under 60s`
