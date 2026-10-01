---
title: Vault Sync
section: 06-ingestion
tags: [ingestion, obsidian, section/ingestion, status/complete, type/spec, vault, watcher]
type: spec
status: complete
implements: src/musubi/vault/watcher.py
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
---
# Vault Sync

Keeping the Obsidian vault (the source of record for curated knowledge) in sync with Qdrant (the derived index). Human edits reach the index, Musubi's own writes (promotion, reflection) reach the index, and neither re-triggers the other.

See [[04-data-model/vault-schema]] for the schema and [[13-decisions/0003-obsidian-as-sor]] for the rationale.

## Two mechanisms

1. **`vault_reconcile`** (`src/musubi/vault/reconciler.py`): a lifecycle-worker job, every 6 hours and once at worker boot. It always runs and is enough on its own; edits land within one interval.
2. **Vault watcher** (`src/musubi/vault/watcher.py`): an **optional** real-time process, `python -m musubi.vault.watcher`, wired by `src/musubi/vault/runtime.py`. The root Compose stack has no watcher service. A systemd unit is provided (`deploy/systemd/musubi-vault-sync.service`) for deployments that want sub-minute sync.

Running the watcher as its own process (not a thread in Core) keeps a crash or a slow boot scan from affecting the API.

## Watcher

### Technologies

- **watchdog** for filesystem events.
- **pydantic** (`CuratedFrontmatter`) for frontmatter validation.
- **ruamel.yaml** for frontmatter parsing and dumping. Comment, key-order and quote-style preservation are not guaranteed yet (the tests for them are skipped; see [[06-ingestion/vault-frontmatter-schema]]).

### Events

Handled: created, modified, moved (processed at the destination path), deleted.

Ignored:

- Any path with a segment starting with `.` or `_` (`.obsidian/`, `.git/`, `_archive/`, `_meta/`, ...).
- Non-`.md` files.
- Markdown files over **10 MB** (`_MAX_VAULT_MD_BYTES`), skipped with a warning. Put large content in the artifact plane instead.

### Debounce and backpressure

Obsidian often saves a file several times in a row. The watcher debounces per path: each event (re)starts a **2-second** timer, and the file is processed once the path is quiet. Debounce state is in memory; pending events are lost on restart, and the boot scan picks them up.

Backpressure, constructor defaults:

- Event intake: token bucket at **10 events/second**. Events beyond that are dropped with a warning.
- Indexing: at most **10** concurrent handlers (`asyncio.Semaphore`).

### Created / modified

```
1. Size gate (10 MB).
2. Parse frontmatter; body_hash = sha256(body).
3. Write-log check: if (path, body_hash) is an unconsumed Core write,
   mark it consumed and stop (echo prevention).
4. No object_id in frontmatter: generate a KSUID, infer the namespace from
   the path, write the frontmatter back (through the write-log) and stop.
   The rewrite is indexed on the next event.
5. Validate CuratedFrontmatter. On failure: log an error and stop.
6. Build the CuratedKnowledge row and CuratedPlane.create(...) (keyed by
   object_id / vault_path).
```

Invalid frontmatter is **logged only**; emitting an ops-alerts Thought is not implemented yet.

### Deleted

The watcher looks up the curated row by `vault_path` and, if exactly one live row matches, transitions it to `archived` through the lifecycle coordinator. A repeat delete of an archived row is a no-op. An ambiguous match (several rows) or a lookup error is refused and logged rather than guessed. Deleting a file never deletes the Qdrant row.

To retire a note while keeping the file, set `state: archived` in its frontmatter or move it into an `_`-prefixed folder such as `_archive/`.

There is no configurable delete behaviour and no restore-from-git mode.

### Boot scan

On start the watcher scrolls `musubi_curated` for `(vault_path, body_hash)`, walks every non-ignored `.md` file in the vault, and re-processes any file whose body hash differs or that has no row. There is no separate watcher state database.

## Echo prevention

The write-log is a small SQLite database shared by every Musubi writer and the watcher:

```
<parent of LIFECYCLE_SQLITE_PATH>/vault-writelog.db
```

Schema (`src/musubi/vault/writelog.py`):

```sql
CREATE TABLE writes (
  file_path TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  written_by TEXT NOT NULL,
  written_at REAL NOT NULL,
  consumed_at REAL DEFAULT NULL,
  PRIMARY KEY (file_path, body_hash)
);
```

Flow:

```
VaultWriter (promotion, reflection, id bootstrap)
  -> record_write(path, body_hash)       before the file is written
  -> write the file

Watcher event -> consume_if_exists(path, body_hash)
  -> matching unconsumed 'core' row: mark consumed, ignore the event
  -> otherwise: process the event
```

`WriteLog` also has `purge_old_entries` (rows older than 1 hour) and `get_orphaned_writes` (unconsumed Core writes older than 5 minutes). No production code path calls them yet, so the table is not pruned automatically. (Not implemented.)

## Reconciler

`vault_reconcile` runs every 6 hours (lock `vault_reconcile.lock`):

```
for each .md file under VAULT_PATH (skipping . and _ segments):
    no object_id in frontmatter        -> skip (the watcher bootstraps ids)
    body_hash unchanged since last pass -> skip
    otherwise                          -> upsert into the curated plane
for each live curated row with a vault_path:
    file missing on disk               -> archive the row (via the coordinator)
```

One file's failure does not abort the pass. The reconciler is idempotent: a second back-to-back run changes nothing. See [[09-operations/asset-matrix]] for the canonical-vs-derived catalog.

## Large files

Large-file chunking is **not implemented**. A curated file is indexed as one row regardless of length (files over 10 MB are skipped by the watcher). Put long reference material in the artifact plane.

## Test Contract

**Module under test:** `src/musubi/vault/watcher.py`, `src/musubi/vault/writer.py`, `src/musubi/vault/writelog.py`, `src/musubi/vault/reconciler.py`

Events (`tests/vault/test_sync.py`):

1. `test_on_created_indexes_new_file`
2. `test_on_modified_reindexes_body_change`
3. `test_on_moved_updates_vault_path`
4. `test_dotfile_ignored`
5. `test_underscore_dir_ignored`
6. `test_oversize_markdown_skipped_with_warning`
7. `test_binary_extension_skipped_with_warning`

Debounce and backpressure:

8. `test_debounce_multiple_rapid_writes_process_once`
9. `test_debounce_extends_on_new_event_during_window`
10. `test_event_rate_limit_drops_with_warning`
11. `test_indexing_rate_limit_backpressure`

Validation:

12. `test_invalid_yaml_emits_thought_and_skips`
13. `test_missing_required_field_emits_thought`
14. `test_missing_object_id_gets_generated_and_written_back`

Echo prevention:

15. `test_writelog_matches_core_write_event_consumed`
16. `test_writelog_mismatch_body_hash_reindexes`
17. `test_writelog_orphan_older_than_5m_logged_as_warning`
18. `test_writelog_entry_purged_after_1h`

Delete (`tests/vault/test_vault003_live_delete.py`):

19. `test_delete_archives_matching_row_via_canonical_transition`
20. `test_repeat_delete_is_idempotent`
21. `test_delete_broken_or_unknown_code_warns_and_refuses`
22. `test_two_namespaces_same_vault_path_neither_archives`

Boot scan (`tests/vault/test_watcher_boot_scan.py`):

23. `test_boot_scan_indexes_new_files`
24. `test_boot_scan_detects_body_hash_change`

Reconciler (`tests/vault/test_reconciler.py`, `tests/vault/test_sync.py`):

25. `test_reconciler_detects_orphan_point`
26. `test_reconciler_detects_orphan_file`
27. `test_reconciler_reindexes_drifted_body_hash`
28. `test_reconciler_idempotent_on_second_run`
29. `test_reconcile_skips_files_without_object_id`
30. `test_reconcile_individual_failure_doesnt_abort_pass`

Skipped:

31. `test_on_modified_frontmatter_only_no_reembed`
32. `test_body_only_no_frontmatter_rejected`
33. `hypothesis: for any sequence of file-system events, Watcher + Reconciler converge to a state where vault ≡ Qdrant`
