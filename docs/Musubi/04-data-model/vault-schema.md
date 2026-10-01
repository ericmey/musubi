---
title: Vault Schema
section: 04-data-model
tags: [data-model, frontmatter, obsidian, section/data-model, status/draft, type/spec, vault]
type: spec
status: draft
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: ["src/musubi/vault/", "tests/vault/"]
---
# Vault Schema

The Obsidian vault is the store of record for curated knowledge. This document defines the on-disk layout, the frontmatter schema, and the rules that let humans and Musubi edit files without stepping on each other.

See [[13-decisions/0003-obsidian-as-sor]] for why Obsidian is SoR.

## Vault root

The vault is the directory at `VAULT_PATH` (default `/var/lib/musubi/vault`, `.env.example:36`). An example layout:

```
$VAULT_PATH/
├── .obsidian/                         # Obsidian config — ignored
├── README.md                          # vault orientation for humans — indexed like any .md
├── alex/
│   └── shared/                        # human-authored notes for alex/shared/curated
│       └── reference/
│           └── cuda-versions.md
├── curated/                           # promotion output
│   └── alex/
│       └── voice/
│           └── projects/
│               └── musubi.md
├── vault/reflections/                 # daily digests from the reflection job
│   └── 2026-04/
│       └── 2026-04-17.md
└── _inbox/                            # untriaged human input — ignored
    └── scratch-2026-04-17.md
```

Rules (`src/musubi/vault/watcher.py:165-195`, `:250-270`):

- Every `.md` file under the vault root is indexed into `musubi_curated`, wherever it sits.
- Any path with a segment starting with `.` or `_` is ignored (`.obsidian/`, `.git/`, `_inbox/`, `_archive/`). There is no flag to index `_inbox/`.
- Non-`.md` files are skipped with a warning; use `POST /v1/artifacts` for binary content.
- Markdown files over 10 MB are skipped with a warning.
- Promotion writes to `curated/<tenant>/<presence>/<primary-topic>/<slug>.md` (`src/musubi/lifecycle/promotion.py:140-159`).
- The reflection job writes `vault/reflections/YYYY-MM/YYYY-MM-DD.md` (`vault_path_for`, `src/musubi/lifecycle/reflection.py:227-232`).

Musubi does not version the vault; putting it under git is an operator choice (see [[04-data-model/curated-knowledge#Backup]]).

## Namespace ↔ path mapping

A file's namespace is its frontmatter `namespace`. When a file has no `object_id`, the watcher bootstraps one and, if needed, infers the namespace from the first two path segments as `<segment-1>/<segment-2>/curated` (`infer_namespace`, `src/musubi/vault/namespacing.py`):

```
path:       alex/shared/reference/cuda-versions.md
namespace:  alex/shared/curated

path:       alex/claude-desktop/runbook.md        (per-presence curated)
namespace:  alex/claude-desktop/curated
```

Inference uses the literal first two segments, so a human file under `curated/alex/...` would be inferred as `curated/alex/curated`. Set `namespace` explicitly in frontmatter for any file outside `<tenant>/<presence>/`.

By convention, most curated knowledge lives in a shared presence such as `alex/shared/curated` (no per-presence siloing for human knowledge). Per-presence curated is supported but used sparingly — it's mainly for presence-specific runbooks.

## Frontmatter schema

YAML frontmatter, enforced by `CuratedFrontmatter` in `src/musubi/vault/frontmatter.py:25-104` (pydantic):

```yaml
---
# Identity (managed by Musubi; humans don't edit these)
object_id: 2W1eP3rZaLlQ4jTuYz0Q9CkZAB1        # KSUID, unique
namespace: alex/shared/curated
schema_version: 1

# Content metadata
title: "CUDA 13 setup notes for the inference host"
topics:
  - infrastructure/gpu
  - projects/musubi
tags: [cuda, nvidia, ubuntu-noble]
importance: 8                                  # 1-10; default 7
summary: |
  One-paragraph summary; when present it is the embedding target instead of the body.

# Lifecycle
state: matured                                 # matured | superseded | archived
version: 3
musubi-managed: false                          # default false; if true, promotion may rewrite

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
    quote: "verify CUDA 13.0 toolchain via nvidia-smi..."   # max 1000 chars
linked_to_topics:
  - infrastructure/networking
contradicts: []
---
```

### Field ownership

| Group | Who edits | Notes |
|---|---|---|
| Identity (`object_id`, `namespace`, `schema_version`) | Musubi only (one-time bootstrap) | Humans must not change these post-creation. |
| Content (`title`, `topics`, `tags`, `importance`, `summary`) | Human (or Musubi if `musubi-managed: true`) | `title` max 200 chars, `summary` max 1000. Tags are lowercased and hyphenated; topics lowercased. |
| Lifecycle (`state`, `version`, `musubi-managed`) | Mixed; `version` is bumped by Musubi on re-index | The indexer does not honor `state` from frontmatter: a new row is inserted as `matured`, and an update leaves the stored state alone. Archive by deleting the file or via `DELETE /v1/curated/{id}`. |
| Temporal (`created`, `updated`, `valid_from`, `valid_until`) | `created`, `updated` — Musubi. `valid_from`, `valid_until` — human. | |
| Lineage | Mostly Musubi | `linked_to_topics` comes from frontmatter only; body wikilinks are not parsed. |

`read_by` is not a curated field: a non-empty `read_by` makes the file fail conversion. Any other unknown key does too (`frontmatter.py:163-166`): the file is logged and not indexed.

### What if a human edits an identity field?

Nothing flags it. The curated plane keys on `(namespace, vault_path)`, so a different `object_id` at the same path is treated as a replacement: the new row is inserted with `supersedes = [old]` and the old row is marked `superseded` (`src/musubi/planes/curated/plane.py:301-330`). Detecting identity edits (warn, alert, stop re-indexing) is planned, not implemented.

## Body content

Markdown body (post-frontmatter) is:

- **Stored verbatim** as the curated `content`, whatever its size (up to the 10 MB watcher limit). The embedding target is `title + summary` when a summary exists, else `title + content`. Chunking large bodies into artifact chunks is planned, not implemented.
- **Wikilinks are not parsed**; they stay in the text.
- **Code blocks preserved** as-is — they're part of the content.
- **Callout blocks** (Obsidian `> [!note]`) preserved; their content is embedded.
- **Images** referenced but not followed (we don't re-embed images as part of the text).

## Echo prevention

Problem: Musubi writes a file → filesystem event → Vault Watcher re-reads the file it just wrote → double-index.

Solution: **write-log shared between Core and Vault Watcher.**

The write-log is `vault-writelog.db`, next to the lifecycle database (the parent directory of `LIFECYCLE_SQLITE_PATH`, `src/musubi/lifecycle/runner.py:648`).

Schema:

```sql
CREATE TABLE IF NOT EXISTS writes (
  file_path TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  written_by TEXT NOT NULL,         -- 'core' | 'human'
  written_at REAL NOT NULL,
  consumed_at REAL DEFAULT NULL,
  PRIMARY KEY (file_path, body_hash)
);
```

When Core writes a curated file (promotion path), it inserts a row **before** the file hits disk. The Vault Watcher's fsevent handler checks the write-log:

```python
if write_log.consume_if_exists(rel_path, body_hash):
    # An unconsumed 'core' row for this (path, body_hash): our own write echoing back.
    # consume_if_exists sets consumed_at.
    return
```

`WriteLog.purge_old_entries` (default 1 hour) and `get_orphaned_writes` (default 5 minutes) exist in `src/musubi/vault/writelog.py`, but nothing schedules them yet.

Promotion never overwrites a human file: if the computed path already holds a `musubi-managed: false` file, promotion writes the sibling `<slug>-promoted-<first 8 chars of concept id>.md`; if it holds a managed file from a different concept, it writes `<slug>-v2.md` (`promotion.py:294-327`). Both emit an `ops-alerts` Thought. A human edit that lands *during* a promotion write is not detected; the later write wins.

## Validation pipeline

On every filesystem event (2s debounce, `watcher.py:223`):

1. **Delete events** archive the row with the stored `vault_path` and stop (`watcher.py:339-460`). Move events are processed at the destination path.
2. **Gates.** Ignore dot/underscore paths, non-`.md` files, files over 10 MB, and files that no longer exist.
3. **Parse.** Split YAML frontmatter from the body and hash the body (SHA256).
4. **Echo check.** If the write-log holds an unconsumed Musubi write for this `(path, body_hash)`, consume it and stop.
5. **Identity bootstrap.** If `object_id` is missing: generate it, set `created`/`updated`/`namespace`, write the file back, and stop (the next event indexes it).
6. **Validate.** Frontmatter must satisfy `CuratedFrontmatter`. Failure is logged and the file is skipped (emitting an ops Thought is a TODO, `watcher.py:307`).
7. **Upsert.** Convert to `CuratedKnowledge` and write it through `CuratedPlane.create`, keyed on `(namespace, vault_path)`: insert, no-op on an unchanged body, in-place update for the same `object_id`, or supersession for a different one (see [[04-data-model/curated-knowledge#Write path]]).
8. **LifecycleEvent.** None of these watcher writes record a LifecycleEvent, except the archive on delete, which goes through `transition()`.

## Obsidian plugin compatibility

Musubi works with stock Obsidian — no special plugin required. We recommend (optional) these plugins for humans:

- **Templater** — for consistent frontmatter templates.
- **Linter** — to normalize YAML frontmatter on save.
- **Dataview** — to query the vault by frontmatter (e.g., "all curated with importance ≥ 8").
- **Tag Wrangler** — for tag hygiene.

Musubi does not rely on any of these — the source of truth is the raw Markdown file + frontmatter.

## Test Contract

**Module under test:** `src/musubi/vault/`, `src/musubi/vault/frontmatter.py`, `src/musubi/vault/watcher.py`

Behaviour checklist; implemented tests are in `tests/vault/` under their own names.

Frontmatter:

1. `test_frontmatter_schema_valid_file_parses`
2. `test_frontmatter_missing_required_field_errors`
3. `test_frontmatter_unknown_fields_block_indexing`
4. `test_frontmatter_datetime_parsed_to_utc`
5. `test_frontmatter_yaml_roundtrip_stable` (write → read → write is identity)

Watcher:

6. `test_watcher_debounces_rapid_saves`
7. `test_watcher_ignores_dotfiles`
8. `test_watcher_ignores_underscore_prefixed_dirs`
9. `test_watcher_writeslog_prevents_double_index`
10. `test_watcher_promotion_writelog_consumed_on_echo`
11. `test_watcher_body_hash_unchanged_is_noop`

Large files:

12. `test_oversize_markdown_skipped`
13. `test_curated_embeds_title_plus_summary_when_summary_present`

Error paths:

14. `test_invalid_yaml_skips_index`
15. `test_body_only_rejected_with_clear_error`

Race conditions:

16. `test_promotion_onto_human_file_writes_sibling_file`
17. `test_rename_updates_vault_path_but_preserves_object_id`

Archival:

18. `test_file_delete_archives_point`
19. `test_underscore_archive_dir_not_reindexed`

Property:

20. `hypothesis: for any valid frontmatter dict, write→read produces an equivalent dict`

Integration:

21. `integration: rebuild curated collection from vault matches live state within 1%`
22. `integration: boot-time vault scan of 10K files completes under 60s`
