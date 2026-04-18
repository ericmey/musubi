---
title: Vault Schema
section: 04-data-model
tags: [data-model, frontmatter, obsidian, section/data-model, status/draft, type/spec, vault]
type: spec
status: draft
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
---
# Vault Schema

The Obsidian vault is the store of record for curated knowledge. This document defines the on-disk layout, the frontmatter schema, and the rules that let humans and Musubi edit files without stepping on each other.

See [[13-decisions/0003-obsidian-as-sor]] for why Obsidian is SoR.

## Vault root

```
/srv/musubi/vault/
├── .git/                              # versioned
├── .obsidian/                         # Obsidian config (theme, plugins, ignore)
├── README.md                          # vault orientation for humans
├── curated/                           # CuratedKnowledge files — indexed
│   ├── eric/
│   │   ├── projects/
│   │   │   ├── musubi.md
│   │   │   └── livekit-agent.md
│   │   ├── infrastructure/
│   │   │   └── gpu.md
│   │   └── index.md
│   ├── _shared/
│   │   └── reference/
│   │       └── cuda-versions.md
│   └── index.md
├── reflections/                       # daily digests — indexed separately
│   └── 2026-04/
│       └── 2026-04-17.md
├── _archive/                          # soft-deleted files — not indexed
│   └── 2026-04-15/
│       └── curated/eric/projects/deprecated.md
└── _inbox/                            # untriaged human input — optionally indexed
    └── scratch-2026-04-17.md
```

Rules:

- Files under `curated/` are indexed into `musubi_curated`.
- Files under `reflections/` are indexed into `musubi_curated` with `tags: [reflection]` and usually `musubi-managed: true`.
- Files under `_archive/` are **not** indexed (prefix `_` means hidden to the Vault Watcher).
- Files under `_inbox/` are optionally indexed based on a config flag (off by default).
- `.git/`, `.obsidian/`, and any file or directory starting with `.` are ignored.

## Namespace ↔ path mapping

```
namespace:  eric/_shared/curated
path:       /srv/musubi/vault/curated/eric/_shared/<...>.md

namespace:  eric/claude-desktop/curated       (rare — per-presence curated)
path:       /srv/musubi/vault/curated/eric/claude-desktop/<...>.md
```

By convention, most curated knowledge lives in `eric/_shared/curated` (no per-presence siloing for human knowledge). Per-presence curated is supported but used sparingly — it's mainly for presence-specific runbooks.

## Frontmatter schema

YAML frontmatter, enforced by `musubi/vault/frontmatter.py` (pydantic):

```yaml
---
# Identity (managed by Musubi; humans don't edit these)
object_id: 2W1eP3rZaLlQ4jTuYz0Q9CkZAB1        # KSUID, unique
namespace: eric/_shared/curated
schema_version: 1

# Content metadata
title: "CUDA 13 setup notes for the musubi host"
topics:
  - infrastructure/gpu
  - projects/musubi
tags: [cuda, nvidia, ubuntu-noble]
importance: 8                                  # 1-10
summary: |
  One-paragraph summary used as embedding target for long files.

# Lifecycle
state: matured                                 # matured | superseded | archived | demoted
version: 3
musubi-managed: false                          # if true, Musubi may auto-edit

# Temporal
created: 2026-04-10T14:22:11Z
updated: 2026-04-17T09:03:55Z
valid_from: 2026-04-10T00:00:00Z               # optional — when the fact became true
valid_until: null                              # optional — when it stopped

# Lineage
supersedes: []                                 # list of KSUIDs
superseded_by: null                            # KSUID or null
promoted_from: null                            # KSUID of source concept, if any
promoted_at: null
merged_from: []
supported_by:                                  # list of ArtifactRefs
  - artifact_id: 2W1eXTxxxxxxxxxxxxxxxxxxx
    chunk_id: 2W1eY8zzzzzzzzzzzzzzzzz
    quote: "verify CUDA 13.0 toolchain via nvidia-smi..."
linked_to_topics:
  - infrastructure/networking
contradicts: []

# Read-state (optional)
read_by: []                                    # list of presences that have read this
---
```

### Field ownership

| Group | Who edits | Notes |
|---|---|---|
| Identity (`object_id`, `namespace`, `schema_version`) | Musubi only (one-time bootstrap) | Humans must not change these post-creation. |
| Content (`title`, `topics`, `tags`, `importance`, `summary`) | Human (or Musubi if `musubi-managed: true`) | |
| Lifecycle (`state`, `version`, `musubi-managed`) | Mixed; `version` is bumped by Musubi on re-index | A human can set `state: archived` to request archival. |
| Temporal (`created`, `updated`, `valid_from`, `valid_until`) | `created`, `updated` — Musubi. `valid_from`, `valid_until` — human. | |
| Lineage | Mostly Musubi; `linked_to_topics` is parsed from `[[wikilinks]]` | |
| Read-state | Musubi only | Updated by presences via the API. |

### What if a human edits an identity field?

Vault Watcher detects a mismatch (`object_id` changed from the last-indexed value). It:

1. Treats the new KSUID as an error (does not re-index as a new object).
2. Logs to the audit log with severity `warn`.
3. Emits a `Thought` to `eric/*` on channel `ops-alerts`: "Object ID was edited; please revert or confirm."
4. Stops re-indexing that file until resolution.

## Body content

Markdown body (post-frontmatter) is:

- **Content-indexed** verbatim for small files (< 2KB).
- **Chunked into artifact chunks** for large files, with the main `musubi_curated` point embedding the title + summary. See [[06-ingestion/vault-sync#large-files]].
- **Wikilinks parsed** (`[[foo]]`, `[[foo|alias]]`, `[[foo#section]]`) to populate `linked_to_topics`.
- **Code blocks preserved** as-is — they're part of the content.
- **Callout blocks** (Obsidian `> [!note]`) preserved; their content is embedded.
- **Images** referenced but not followed (we don't re-embed images as part of the text).

## Echo prevention

Problem: Musubi writes a file → filesystem event → Vault Watcher re-reads the file it just wrote → double-index.

Solution: **write-log shared between Core and Vault Watcher.**

```
/srv/musubi/vault-state/write-log.sqlite
```

Schema:

```sql
CREATE TABLE IF NOT EXISTS writes (
  file_path TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  written_by TEXT NOT NULL,         -- 'core' | 'human'
  written_at REAL NOT NULL,
  PRIMARY KEY (file_path, body_hash)
);
```

When Core writes a curated file (promotion path), it inserts a row **before** the file hits disk. The Vault Watcher's fsevent handler checks the write-log:

```python
if (path, body_hash) in write_log and written_by == "core":
    # This is our own write echoing back.
    consume(path, body_hash)
    return
```

Entries are purged after 5 minutes.

If two actors race — human edits a file while Core is promoting to the same path — the human wins; Core's write is detected as a conflict (via `body_hash` mismatch at commit time), aborts with `VaultWriteConflict`, and the promotion writes to a sibling file `<slug>-promoted-<short-ksuid>.md`. The operator can later merge by hand.

## Validation pipeline

On every filesystem event (2s debounce):

1. **File exists check.** Rename or delete events are handled separately.
2. **YAML parse.** Failure → log error, emit `Thought` to operator, skip indexing.
3. **Pydantic validate.** Frontmatter must satisfy `CuratedFrontmatter` model. Unknown fields are preserved; missing required fields produce an error.
4. **Identity check.** If `object_id` is missing: generate, write-back (via the Vault Watcher with `written_by=core` in the log).
5. **Body hash compute.** SHA256 of content bytes (post-frontmatter). If unchanged from last-indexed `body_hash`: no-op (idempotency).
6. **Embedding.** Re-embed only if body changed; keep existing embedding if only frontmatter-meta changed.
7. **Upsert.** Update the `musubi_curated` point.
8. **LifecycleEvent.** Emit if `state` changed.

## Obsidian plugin compatibility

Musubi works with stock Obsidian — no special plugin required. We recommend (optional) these plugins for humans:

- **Templater** — for consistent frontmatter templates.
- **Linter** — to normalize YAML frontmatter on save.
- **Dataview** — to query the vault by frontmatter (e.g., "all curated with importance ≥ 8").
- **Tag Wrangler** — for tag hygiene.

Musubi does not rely on any of these — the source of truth is the raw Markdown file + frontmatter.

## Test contract

**Module under test:** `musubi/vault/`, `musubi/vault/frontmatter.py`, `musubi/vault/watcher.py`

Frontmatter:

1. `test_frontmatter_schema_valid_file_parses`
2. `test_frontmatter_missing_required_field_errors`
3. `test_frontmatter_unknown_fields_preserved_on_rewrite`
4. `test_frontmatter_datetime_parsed_to_utc`
5. `test_frontmatter_yaml_roundtrip_stable` (write → read → write is identity)

Watcher:

6. `test_watcher_debounces_rapid_saves`
7. `test_watcher_ignores_dotfiles`
8. `test_watcher_ignores_underscore_prefixed_dirs`
9. `test_watcher_writeslog_prevents_double_index`
10. `test_watcher_promotion_writelog_consumed_on_echo`
11. `test_watcher_identity_change_logs_warning_and_skips`
12. `test_watcher_body_hash_unchanged_is_noop`

Large files:

13. `test_large_file_chunks_body_into_artifact`
14. `test_large_file_curated_embeds_title_plus_summary`

Error paths:

15. `test_invalid_yaml_emits_thought_and_skips_index`
16. `test_body_only_rejected_with_clear_error`

Race conditions:

17. `test_concurrent_human_edit_and_core_promotion_writes_sibling_file`
18. `test_rename_updates_vault_path_but_preserves_object_id`

Archival:

19. `test_soft_delete_moves_file_and_archives_point`
20. `test_underscore_archive_dir_not_reindexed`

Property:

21. `hypothesis: for any valid frontmatter dict, write→read produces an equivalent dict`

Integration:

22. `integration: rebuild curated collection from vault matches live state within 1%`
23. `integration: boot-time vault scan of 10K files completes under 60s`
