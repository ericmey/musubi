---
title: Curated Knowledge
section: 04-data-model
tags: [curated, data-model, obsidian, schema, section/data-model, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
---
# Curated Knowledge

Topic-first, human-authoritative, durable facts. The Obsidian vault is the **store of record**. Qdrant is a derived index.

## Pydantic model

```python
# musubi/types/curated.py

class CuratedKnowledge(BaseModel):
    object_id: KSUID
    namespace: str                      # e.g., "eric/_shared/curated"
    schema_version: int = 1

    title: str
    content: str                        # the markdown body (after frontmatter)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    topics: list[str]                   # e.g., ["projects/musubi", "infrastructure/gpu"]
    importance: int = Field(default=7, ge=1, le=10)  # curated defaults higher

    # Temporal
    created_at: datetime
    created_epoch: float
    updated_at: datetime
    updated_epoch: float

    # Bitemporal (optional; for facts with explicit validity)
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    # Lifecycle
    version: int = 1
    state: LifecycleState = "matured"   # curated never starts "provisional"

    # Vault binding
    vault_path: str                     # relative to vault root, e.g., "curated/eric/projects/musubi.md"
    body_hash: str                      # sha256 of content (post-frontmatter)
    musubi_managed: bool                # False = human-only write; True = auto-promotion wrote
    file_size_bytes: int

    # Lineage
    promoted_from: KSUID | None = None  # if True, the source concept
    promoted_at: datetime | None = None
    supersedes: list[KSUID] = Field(default_factory=list)
    superseded_by: KSUID | None = None
    merged_from: list[KSUID] = Field(default_factory=list)
    supported_by: list[ArtifactRef] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)
    linked_to_topics: list[str] = Field(default_factory=list)  # topical cross-refs

    # Read-state (per-presence, like thoughts — optional)
    read_by: list[str] = Field(default_factory=list)
```

## Vault file format

```markdown
---
object_id: 2W1eP3rZaLlQ4jTuYz0Q9CkZAB1
namespace: eric/_shared/curated
schema_version: 1
title: "CUDA 13 setup notes for the musubi host"
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

# CUDA 13 setup notes for the musubi host

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
- **`vault-path` is derived at index time**, not stored in frontmatter (it's just the file's location).
- **`body_hash` is stored in Qdrant only**, not in frontmatter — it's derived. Prevents humans from being confused by a "managed" field they shouldn't edit.

## Authorization

A file is writable by a Musubi process (Lifecycle Worker / Core promotion) iff:

- `musubi-managed: true` in frontmatter.

If a human flips a file from `true` to `false` (e.g., they adopted a promoted file and want to take over), Musubi stops auto-editing. From that point the file is human-authored; promotions targeting the same topic create a *new* file.

If `musubi-managed: false` and a system process tries to write: the operation fails with `VaultWriteDenied` and is logged to audit. This is an invariant, enforced in `musubi/vault/writer.py`.

## Qdrant layout

Collection: `musubi_curated`.

**Named vectors:** same as episodic (`dense_bge_m3_v1`, `sparse_splade_v1`), dimensions identical so hybrid-search parameters are shared.

**Embedding target:** title + summary if present, else title + first 2048 tokens of content. We don't embed the whole file — large curated docs are chunked into `ArtifactChunk` entries if they exceed 2KB, with the CuratedKnowledge itself pointing via `supported_by`. See [[06-ingestion/vault-sync#large-files]].

**Payload indexes:** (delta from episodic)

| Field | Type | Purpose |
|---|---|---|
| `namespace`, `object_id`, `state`, `tags`, `linked_to_topics` | KEYWORD | (same as episodic) |
| `topics` | KEYWORD | topic-key queries |
| `importance`, `version`, `read_by` | various | standard |
| `valid_from_epoch`, `valid_until_epoch` | FLOAT | bitemporal queries |
| `musubi_managed` | BOOL | filter auto-managed |
| `vault_path` | KEYWORD | reverse lookup |

## Storage semantics

### Read path

A read always comes from **Qdrant** (fast). If the content field is truncated (we only index summaries for large docs), the API caller can request the full file by ID:

```
GET /v1/curated-knowledge/{id}?include=body
```

which reads from the vault filesystem by `vault_path`.

### Write path

**Primary: human edits in Obsidian.**
1. Human saves `vault/curated/eric/projects/musubi.md`.
2. Filesystem event → Vault Watcher (2s debounce).
3. Watcher reads file, parses frontmatter, validates schema.
4. If the file lacks `object_id`: generate one, write frontmatter back (this IS a write by Musubi; flag `musubi-managed: true` is *not* set automatically — the file remains human-managed; we just bootstrapped the id). Record the write in the write-log so the echo event is ignored.
5. If `body_hash` of the current content differs from the last-indexed hash: re-embed, upsert Qdrant point. Bump `updated_at`, `version`.
6. If the file is new: insert Qdrant point, new KSUID.
7. If the file is moved: update `vault_path`.
8. If the file is deleted: mark Qdrant point `state = "archived"`, move markdown file to `vault/_archive/<date>/...` (soft-delete).

**Secondary: promotion from synthesis.**
1. Lifecycle Worker picks a synthesized concept eligible for promotion.
2. Worker generates markdown body + frontmatter via LLM (Ollama).
3. Worker calls Core's internal `curated_create_from_concept(concept, rendered)`.
4. Core validates the rendering, computes path, writes file with `musubi-managed: true`, writes write-log entry.
5. Core upserts Qdrant point.
6. Vault Watcher sees the file write, finds the write-log entry, skips re-index (already indexed in step 5).

### Delete

Deletes are **soft** by default:
- File moved to `vault/_archive/YYYY-MM-DD/<original-path>`.
- Qdrant point: `state = "archived"`.
- Operator scope required for **hard delete** (remove file + Qdrant point + lineage rewrites).

## Test contract

**Module under test:** `musubi/planes/curated/` + `musubi/vault/`

1. `test_read_from_qdrant_returns_indexed_fields`
2. `test_read_with_include_body_reads_from_vault_filesystem`
3. `test_human_edit_triggers_reindex_after_debounce`
4. `test_reindex_updates_body_hash_and_version`
5. `test_identical_content_save_no_index_write` (idempotency)
6. `test_file_move_updates_vault_path_in_qdrant`
7. `test_file_delete_archives_and_marks_state`
8. `test_frontmatter_missing_object_id_gets_generated_and_written_back`
9. `test_frontmatter_schema_invalid_file_is_not_indexed_and_emits_thought`
10. `test_musubi_managed_true_file_accepts_system_write`
11. `test_musubi_managed_false_file_rejects_system_write`
12. `test_write_log_echo_detection_prevents_double_index`
13. `test_promotion_writes_file_and_index_atomically_enough`
14. `test_promotion_links_concept_to_curated_via_promoted_to_and_promoted_from`
15. `test_large_file_chunks_body_as_artifact_and_references`
16. `test_bitemporal_valid_until_excludes_from_default_query`
17. `test_supersession_chain_read_returns_latest`
18. `test_cross_namespace_reference_logged_in_audit`
19. `test_isolation_read_enforcement` (inherited from namespace)
20. `test_hard_delete_requires_operator_scope`

Property tests:

21. `hypothesis: vault_path <-> object_id is a bijection for non-archived files at any given time`
22. `hypothesis: body_hash changes iff content bytes change (ignoring frontmatter)`

Integration:

23. `integration: rebuild_curated_from_vault matches live state within 1%` (catches index drift)
24. `integration: concurrent human edit + promotion write to same path produces a deterministic winner`

## Edge cases

- **Frontmatter with unknown fields:** accepted, preserved on rewrite, not indexed.
- **Wikilinks in body `[[foo]]`:** parsed to extract `linked_to_topics` references; added to payload. Broken wikilinks logged but not an error.
- **File with only frontmatter, no body:** rejected at index time (minimum content length).
- **File outside `vault/curated/`:** ignored by Watcher (we only index the curated directory tree).
- **Symbolic links in vault:** followed, but we don't write through them.

## Backup

The vault is git-versioned. Nightly auto-commit job:

```bash
cd /srv/musubi/vault && git add -A && git commit -m "autosave $(date -Iseconds)" || true
git push origin main
```

This gives us:
- Every edit history-preserved.
- Simple revert on accidental corruption.
- Offsite backup if remote is configured.

See [[09-operations/backup-restore]].
