---
title: Vault Frontmatter Schema
section: 06-ingestion
tags: [frontmatter, ingestion, schema, section/ingestion, status/complete, type/spec, vault]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Vault Frontmatter Schema

The pydantic model enforced on every curated markdown file's YAML frontmatter. This is the contract between humans and Musubi. Authoring guidelines live in the vault README; this file is the normative spec.

See also [[04-data-model/vault-schema]] for the on-disk layout and authorization story.

## Model

```python
# musubi/vault/frontmatter.py

class CuratedFrontmatter(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Identity
    object_id: KSUID
    namespace: str = Field(pattern=r"^[a-z0-9-]+/[a-z0-9-_]+/[a-z]+$")
    schema_version: int = 1

    # Content metadata
    title: str = Field(min_length=1, max_length=200)
    topics: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    importance: int = Field(default=7, ge=1, le=10)
    summary: str | None = Field(default=None, max_length=1000)

    # Lifecycle
    state: LifecycleState = "matured"
    version: int = Field(default=1, ge=1)
    musubi_managed: bool = Field(default=False, alias="musubi-managed")

    # Temporal
    created: datetime
    updated: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    # Lineage
    supersedes: list[KSUID] = Field(default_factory=list)
    superseded_by: KSUID | None = None
    promoted_from: KSUID | None = None
    promoted_at: datetime | None = None
    merged_from: list[KSUID] = Field(default_factory=list)
    supported_by: list[ArtifactRefFrontmatter] = Field(default_factory=list)
    linked_to_topics: list[str] = Field(default_factory=list)
    contradicts: list[KSUID] = Field(default_factory=list)

    # Read state (optional, rare in frontmatter)
    read_by: list[str] = Field(default_factory=list)


class ArtifactRefFrontmatter(BaseModel):
    artifact_id: KSUID
    chunk_id: KSUID | None = None
    quote: str | None = Field(default=None, max_length=1000)
```

`extra="allow"` means humans can add their own keys (e.g., `my_custom_tag: foo`), and Musubi preserves them on rewrite. They're not indexed.

## Enforcement rules

### 1. Required fields on first write

New files (from humans) need only:

- `title`

Everything else is optional on initial creation. Watcher will:

- Generate `object_id` and write it back (flagged `written_by=core` in write-log to avoid double-index).
- Compute `namespace` from the file's vault path.
- Set `created = updated = now`.
- Default `state = "matured"`, `version = 1`.
- Leave `musubi_managed = false`.

This minimal-authoring flow keeps curated creation frictionless.

### 2. Immutable fields (humans shouldn't edit)

- `object_id`
- `namespace` (unless moving between namespace directories — treat that as rename+reindex)
- `schema_version`

If a human edits these: Watcher logs an error, emits a Thought on `ops-alerts`, does not re-index the file. Operator resolves.

### 3. System-managed vs human-managed behavior

- `musubi-managed: true` → Musubi (promotion path) may write this file, re-index, mutate the body. Humans can still edit; the next Musubi write will overwrite (so `musubi-managed: true` implies "I trust Musubi here; I'll edit only if I know what I'm doing").
- `musubi-managed: false` → Musubi **will not** edit the body. It may still update the Qdrant index (body_hash, read_by, etc.) but never modifies the file's content.

Flipping true → false is supported: write `musubi-managed: false`, save. Future promotions that would have overwritten this file create a sibling instead.

Flipping false → true is unusual; we log a warning and proceed.

### 4. Timestamp handling

- Parsed as ISO8601. Timezone-aware required; Watcher rejects bare datetimes (e.g., `2026-04-17T09:00:00` without `Z` or offset).
- Stored as UTC internally.
- Serialized with explicit `Z` suffix on write.

### 5. Tag and topic canonicalization

- Tags: lowercased, stripped, hyphenated on write. Duplicates removed. Aliases resolved (per `config/tag-aliases.yaml`).
- Topics: preserved as-is (hierarchical strings), but lowercased. No dedup (order matters — first is "primary").

Canonicalization happens at index time, not edit time — the human's YAML stays as they typed it, and Watcher normalizes when pushing to Qdrant.

### 6. Wikilink parsing

Body wikilinks (`[[foo]]`, `[[foo|alias]]`, `[[foo#section]]`) are parsed to populate `linked_to_topics`. Targets are extracted as bare topic strings. Broken targets (point to non-existent files) are allowed — just recorded as broken in a Watcher log.

### 7. Valid_until soft-expire

Files with `valid_until` in the past are indexed but excluded from default retrieval (same as `state: archived`). The Qdrant point stays — forensics and bitemporal queries need it.

## YAML formatting rules

We use `ruamel.yaml` for round-tripping. This preserves:

- Comments (humans commenting inside frontmatter).
- Key ordering.
- Block vs flow style choices.
- Quoted vs unquoted strings.

Unpreserved (normalized on rewrite):

- Extra whitespace around `:`.
- Trailing spaces.
- Non-ASCII punctuation quirks.

## Validation errors

When a file has invalid frontmatter:

```
vault/curated/eric/_shared/projects/musubi.md — validation failed:
  importance: value 15 > 10
  created: must be timezone-aware
  object_id: must be a valid KSUID (27 chars)
```

Emitted as:

- Log entry at ERROR level.
- Thought on `ops-alerts` (channel) with the file path + error lines.
- Entry in the Watcher's `last-errors.json` (last 100 errors; readable via API).

The file is **not** indexed until the error is resolved.

## Human-facing templates

Recommended Obsidian Templater snippets live in `vault/_meta/templates/`:

```yaml
# vault/_meta/templates/new-curated.md
---
title: "{{title}}"
topics:
  - "<< topic >>"
tags: []
importance: 7
valid_from: "{{date:YYYY-MM-DD}}T00:00:00Z"
---

# {{title}}

{{cursor}}
```

Documented in `vault/README.md`. Musubi does not require Templater; it's a UX nicety.

## Examples

### Minimal human-authored file

```markdown
---
title: "Deploy LiveKit agent"
---

# Deploy LiveKit agent

Steps:
1. ...
```

After Watcher processes: `object_id`, `namespace`, `created`, `updated`, etc. are populated automatically (via the bootstrap write).

### Fully populated curated file

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
created: 2026-04-10T14:22:11Z
updated: 2026-04-17T09:03:55Z
valid_from: 2026-04-10T00:00:00Z
supported_by:
  - {artifact_id: 2W1eX..., chunk_id: 2W1eY..., quote: "driver version 575 confirmed"}
---

# CUDA 13 setup notes

...
```

### Musubi-promoted file

```markdown
---
object_id: 2W1fA...
namespace: eric/_shared/curated
title: "CUDA 13 installation pattern"
topics:
  - infrastructure/gpu
tags: [cuda, pattern]
importance: 7
state: matured
musubi-managed: true
promoted_from: 2W1eC...
promoted_at: 2026-04-16T04:00:02Z
merged_from: [2W1eA..., 2W1eB..., 2W1eD..., 2W1eE..., 2W1eF...]
created: 2026-04-16T04:00:02Z
updated: 2026-04-16T04:00:02Z
---

# CUDA 13 installation pattern

...
```

## Test contract

**Module under test:** `musubi/vault/frontmatter.py`

Parsing:

1. `test_minimal_file_with_only_title_parses`
2. `test_fully_populated_file_parses`
3. `test_missing_title_errors`
4. `test_extra_fields_preserved_in_output`
5. `test_naive_datetime_rejected`
6. `test_importance_out_of_range_errors`
7. `test_invalid_ksuid_errors`

Round-trip:

8. `test_yaml_comments_preserved`
9. `test_key_order_preserved`
10. `test_quoted_string_style_preserved`

Normalization:

11. `test_tags_lowercased_on_write`
12. `test_tag_aliases_applied_on_write`
13. `test_datetime_serialized_with_z`

Authorization:

14. `test_musubi_managed_true_allows_system_write`
15. `test_musubi_managed_false_blocks_system_write`
16. `test_musubi_managed_flag_flip_respected_next_promotion`

Identity:

17. `test_bootstrap_object_id_writes_frontmatter_back`
18. `test_object_id_edit_by_human_logged_and_skipped`

Examples:

19. `test_example_minimal_file_equivalent_after_roundtrip`
20. `test_example_musubi_promoted_file_equivalent`

Integration:

21. `integration: create minimal file via editor simulation, watcher bootstraps object_id, file reread stable`
22. `integration: invalid frontmatter file → Thought emitted, no Qdrant change, `last-errors.json` updated`
