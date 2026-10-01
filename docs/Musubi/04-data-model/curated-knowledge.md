---
title: Curated Knowledge
section: 04-data-model
tags: [curated, data-model, obsidian, schema, section/data-model, status/complete, type/spec]
type: spec
status: complete
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: "tests/planes/test_curated.py"
---
# Curated Knowledge

Topic-first, human-authoritative, durable facts. The Obsidian vault is the **store of record**. Qdrant is a derived index.

## Pydantic model

The model is `CuratedKnowledge` in `src/musubi/types/curated.py:20-51`, extending `MemoryObject` (`src/musubi/types/base.py:108-184`). Inherited fields are listed in [[04-data-model/object-hierarchy]]. Curated-specific:

| Field | Type | Default / rule |
|---|---|---|
| `state` | `matured \| superseded \| archived` | `matured`; curated never starts `provisional` |
| `title` | `str`, non-empty | required |
| `topics` | `list[str]`, e.g. `["projects/musubi", "infrastructure/gpu"]` | `[]` |
| `vault_path` | `str`, relative to the vault root | required |
| `body_hash` | sha256 hex of the markdown body (frontmatter excluded) | required |
| `musubi_managed` | `bool` | `True` on the model; `False` when parsed from frontmatter that omits it |
| `promoted_from` / `promoted_at` | `KSUID` / `datetime` | `None`; `promoted_at` is required whenever `promoted_from` is set |

`importance` defaults to 5 on the model (inherited) but to 7 when parsed from vault frontmatter (`src/musubi/vault/frontmatter.py:42`), so human-authored notes start higher. There is no `file_size_bytes` field, and `read_by` is not a curated field: frontmatter carrying a non-empty `read_by` is rejected (`frontmatter.py:163-164`).

## Vault file format

```markdown
---
object_id: 2W1eP3rZaLlQ4jTuYz0Q9CkZAB1
namespace: alex/shared/curated
schema_version: 1
title: "CUDA 13 setup notes for the inference host"
topics:
  - infrastructure/gpu
  - projects/musubi
tags: [cuda, nvidia, ubuntu-noble]
importance: 8
state: matured
version: 3
musubi-managed: false
valid_from: 2026-04-10T00:00:00Z
created: 2026-04-10T14:22:11Z
updated: 2026-04-17T09:03:55Z
supersedes: []
supported_by:
  - {artifact_id: 2W1eXTxxxxxxxxxxxxxxxxxxx, chunk_id: 2W1eY8zzzzzzzzzzzzzzzzz}
linked_to_topics: [infrastructure/networking]
---

# CUDA 13 setup notes for the inference host

Install the NVIDIA driver 575 series, verify CUDA 13.0 toolchain, install
`nvidia-container-toolkit` for Docker GPU access.

[[infrastructure/nvidia-container-toolkit]]

## Driver installation
...
```

Key rules:

- **Filename is `<slug>.md`** where slug is a stable, kebab-cased derivative of title. Renames require a migration.
- **Frontmatter is YAML, not TOML.** Obsidian native support.
- **`object_id` is the id of record**, not the filename. The file can be renamed; the KSUID persists.
- **`vault_path` is derived at index time**, not stored in frontmatter (it's just the file's location).
- **`body_hash` is stored in Qdrant only**, not in frontmatter — it's derived. Prevents humans from being confused by a "managed" field they shouldn't edit.

## Authorization

Promotion only overwrites a file that is `musubi-managed: true` **and** was promoted from the same concept (`promoted_from` matches); that rewrite reuses the file's `object_id`. Every other conflict at the computed path produces a sibling file instead of an overwrite (`src/musubi/lifecycle/promotion.py:294-327`):

- `musubi-managed: true` but promoted from a different concept: `<slug>-v2.md`.
- `musubi-managed: false` (human-authored): `<slug>-promoted-<first 8 chars of concept id>.md`.

Both cases emit an `ops-alerts` Thought. If a human flips a file from `true` to `false` (e.g., they adopted a promoted file and want to take over), promotion stops rewriting it.

`VaultWriter.write_curated` (`src/musubi/vault/writer.py`) itself does not check `musubi-managed`; the protection is the promotion path's conflict check. It does refuse any path that resolves outside the vault root.

## Qdrant layout

Collection: `musubi_curated`.

**Named vectors:** same as episodic (`dense_bge_m3_v1`, `sparse_splade_v1`), dimensions identical so hybrid-search parameters are shared.

**Embedding target:** `title` + `summary` when a summary is present, else `title` + `content` (`_embed_target`, `src/musubi/planes/curated/plane.py:147-155`). Chunking large curated bodies into `ArtifactChunk` rows is planned, not implemented. The watcher skips markdown files over 10 MB (`src/musubi/vault/watcher.py:35`).

**Payload indexes:** the universal set plus the curated deltas in `src/musubi/store/specs.py:175-185` (`vault_path`, `musubi_managed`, `valid_from_epoch`, `valid_until_epoch`, `promoted_from`, `supersedes`, `superseded_by`, `body_hash`, `read_by`). See [[04-data-model/qdrant-layout#Payload indexes]].

## Storage semantics

### Read path

A read always comes from **Qdrant**: `GET /v1/curated/{id}` and `GET /v1/curated` (list). The stored `content` is the full markdown body. There is no option to read the body back from the vault file through the API.

### Write path

**Primary: human edits in Obsidian.**
1. Human saves a file under the vault, e.g. `alex/shared/projects/musubi.md`.
2. Filesystem event → Vault Watcher (2s debounce). Paths with any segment starting with `.` or `_`, and non-`.md` files, are ignored (`watcher.py:170-180`).
3. Watcher reads file, parses frontmatter, validates schema. A file that fails validation is logged and not indexed.
4. If the file lacks `object_id`: generate one, set `created`/`updated`, infer `namespace` from the first two path segments (`<tenant>/<presence>/curated`, `src/musubi/vault/namespacing.py`) and write the frontmatter back. `musubi-managed` is *not* set; the file remains human-managed. The write-back is recorded in the write-log, and the next event indexes the file.
5. Otherwise, unless the write-log marks the event as Musubi's own echo, the watcher builds a `CuratedKnowledge` from the frontmatter and calls `CuratedPlane.create`, which keys on `(namespace, vault_path)` (`src/musubi/planes/curated/plane.py:214-330`):
   - no row at that path: insert at `state = "matured"`, `version = 1`;
   - same `body_hash`: no-op;
   - same `object_id`, new body: in-place update of the author fields, `version + 1`, re-embedded when the embedding text changed;
   - different `object_id` at the same path: insert the new row with `supersedes = [old]` and mark the old row `superseded`.
6. A move is processed as a write at the destination path. How the existing row follows a rename is not verified; treat renames as unverified.
7. If the file is deleted: the matching row (by stored `vault_path`) transitions to `state = "archived"` (`watcher.py:339-460`). The file is not moved anywhere; it is already gone.

**Secondary: promotion from synthesis.**
1. Lifecycle Worker picks a synthesized concept eligible for promotion.
2. Worker renders the markdown body via the LLM.
3. Worker computes the path `curated/<tenant>/<presence>/<primary-topic>/<slug>.md` (`compute_path`, `promotion.py:140-159`) and resolves conflicts as described under Authorization.
4. Worker writes the file with `musubi-managed: true` through `VaultWriter`, which records a write-log entry first.
5. Worker upserts the curated Qdrant point and transitions the concept to `promoted`.
6. Vault Watcher sees the file write, finds the write-log entry, and skips re-index.

### Delete

- **File deleted in the vault:** the row transitions to `archived` (see step 7 above).
- **`DELETE /v1/curated/{id}?namespace=...`:** write scope; transitions the row to `archived` (`src/musubi/api/routers/writes_curated.py:206-240`). It does not touch the vault file.
- Moving files to an `_archive/` folder, and a hard delete that removes file + point + lineage, are planned, not implemented.

## Test Contract

**Module under test:** `src/musubi/planes/curated/` + `src/musubi/vault/`

Behaviour checklist; the implemented tests are in `tests/planes/test_curated.py` (it lists the bullets it covers) and `tests/vault/`.

1. `test_read_from_qdrant_returns_indexed_fields`
2. `test_human_edit_triggers_reindex_after_debounce`
3. `test_reindex_updates_body_hash_and_version`
4. `test_identical_content_save_no_index_write` (idempotency)
5. `test_file_move_updates_vault_path_in_qdrant`
6. `test_file_delete_archives_and_marks_state`
7. `test_frontmatter_missing_object_id_gets_generated_and_written_back`
8. `test_frontmatter_schema_invalid_file_is_not_indexed` (emitting an ops Thought here is a TODO, `src/musubi/vault/watcher.py:307`)
9. `test_promotion_rewrites_own_managed_file`
10. `test_promotion_conflict_with_human_file_writes_sibling`
11. `test_write_log_echo_detection_prevents_double_index`
12. `test_promotion_writes_file_and_index_atomically_enough`
13. `test_promotion_links_concept_to_curated_via_promoted_to_and_promoted_from`
14. `test_bitemporal_valid_until_excludes_from_default_query`
15. `test_supersession_chain_read_returns_latest`
16. `test_isolation_read_enforcement` (inherited from namespace)

Property tests:

17. `hypothesis: vault_path <-> object_id is a bijection for non-archived files at any given time`
18. `hypothesis: body_hash changes iff content bytes change (ignoring frontmatter)`

Integration:

19. `integration: rebuild_curated_from_vault matches live state within 1%` (catches index drift)
20. `integration: concurrent human edit + promotion write to same path produces a deterministic winner`

## Edge cases

- **Frontmatter with unknown fields:** the file parses, but conversion to `CuratedKnowledge` refuses unsupported fields (`frontmatter.py:165-166`), so the file is not indexed. The error is logged.
- **Wikilinks in body `[[foo]]`:** not parsed. `linked_to_topics` comes from frontmatter only.
- **File with only frontmatter, no body:** rejected at index time (`content` must be non-empty).
- **Files the watcher ignores:** any path with a segment starting with `.` or `_` (so `_inbox/`, `_archive/`, `.obsidian/`), and any non-`.md` file. Every other `.md` file under the vault root is indexed; there is no restriction to a `curated/` subtree.

## Backup

The vault is a plain directory at `VAULT_PATH` (default `/var/lib/musubi/vault`, `.env.example:36`). Musubi does not version it. Putting it under git (for example a scheduled `git add -A && git commit`) is an operator choice that gives edit history, simple revert, and an offsite copy if a remote is configured.

See [[09-operations/backup-restore]].
