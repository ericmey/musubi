---
title: Vault Frontmatter Schema
section: 06-ingestion
tags: [frontmatter, ingestion, schema, section/ingestion, status/complete, type/spec, vault]
type: spec
status: complete
implements: src/musubi/vault/frontmatter.py
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
---
# Vault Frontmatter Schema

The pydantic model enforced on every curated markdown file's YAML frontmatter. It is the contract between people editing the vault and Musubi. This page is the normative spec.

See also [[04-data-model/vault-schema]] for the on-disk layout and authorization story.

## Model

```python
# src/musubi/vault/frontmatter.py

class CuratedFrontmatter(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # Identity
    object_id: KSUID | None = None          # empty string is treated as missing
    namespace: str | None = Field(default=None, pattern=r"^[a-z0-9-]+/[a-z0-9-_]+/[a-z]+$")
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

`extra="allow"` lets people add their own keys (for example `my_custom_tag: foo`). They are kept when Musubi rewrites the file, and they are not indexed.

## Enforcement rules

### 1. Minimal authoring and ID bootstrap

A new file needs no frontmatter fields at all. When the [[06-ingestion/vault-sync|vault watcher]] sees a markdown file without an `object_id`, it:

- generates an `object_id` (KSUID);
- infers `namespace` from the path: the first two path segments become `tenant/presence`, giving `<tenant>/<presence>/curated` (`src/musubi/vault/namespacing.py`). A file at the vault root falls back to `system/internal/curated`;
- sets `created = updated = now`;
- uses the file name as `title` if none is given;
- leaves `musubi-managed` false;
- writes the frontmatter back through the write-log, so the rewrite is not treated as a human edit.

The 6-hourly reconciler does not bootstrap ids; it skips files without an `object_id`. Without the watcher running, add `object_id`, `namespace`, `title`, `created` and `updated` yourself.

### 2. Fields people should not edit

- `object_id`
- `namespace` (moving a file between namespace directories should be treated as a rename plus re-index)
- `schema_version`

Editing these is **not detected** today: the watcher validates and indexes whatever it finds. (Detection and an ops-alerts Thought are not implemented.)

### 3. Musubi-managed vs human-managed

- `musubi-managed: true`: the promotion path may rewrite this file. People can still edit it, but the next promotion of the same concept overwrites it.
- `musubi-managed: false`: promotion never overwrites the file. If its target path holds a human-managed file, it writes a sibling `<slug>-promoted-<id>.md` instead (see [[06-ingestion/promotion#Path conflicts]]). Changes to the file still reach the index.

Flipping `true` to `false` is the way to take a promoted note over by hand.

### 4. Timestamps

- Parsed as ISO 8601. A datetime without a timezone is rejected.
- Serialized in ISO 8601 with an offset when Musubi writes the file.

### 5. Tags and topics

On validation:

- Tags are lowercased, stripped, spaces become hyphens, and duplicates are removed (order kept).
- Topics are lowercased; order is kept (the first is the primary topic used by promotion paths).

Tag **aliases are not applied** to vault files; the alias map in `src/musubi/lifecycle/maturation.py` (`DEFAULT_TAG_ALIASES`) is used only by episodic maturation.

### 6. Wikilinks

Body wikilinks are **not parsed**. `linked_to_topics` is indexed exactly as written in frontmatter.

### 7. `valid_until` soft-expire

A row whose `valid_until` is in the past is still indexed, but the hybrid retrieval default view excludes it (`valid_until` at or before the query time). The row stays for forensics and bitemporal queries.

## YAML handling

Frontmatter is parsed and written with `ruamel.yaml` (quotes preserved on load, 2-space mappings, no line wrapping). When Musubi rewrites a file it serializes the validated model, so:

- custom keys survive;
- comments, original key order and original quoting style are **not** guaranteed to survive (the tests for them are skipped);
- keys with empty values are dropped.

## Validation errors

When a file's frontmatter fails validation, the watcher logs it at ERROR level with the path and the pydantic error, and does not index that version of the file:

```
Frontmatter validation failed for alex/shared/projects/musubi.md: 1 validation error for CuratedFrontmatter
importance
  Input should be less than or equal to 10
```

There is no ops-alerts Thought and no `last-errors.json` for validation failures. (Not implemented.)

## Suggested template

Musubi does not ship or require an Obsidian template. A minimal one that validates without the watcher's bootstrap:

```yaml
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

## Examples

### Minimal human-authored file

```markdown
---
title: "Deploy the voice agent"
---

# Deploy the voice agent

Steps:
1. ...
```

After the watcher processes it, `object_id`, `namespace`, `created` and `updated` are filled in by the bootstrap write.

### Fully populated curated file

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
namespace: alex/shared/concept
title: "CUDA 13 installation pattern"
topics:
  - infrastructure/gpu
tags: [cuda, pattern]
importance: 7
state: matured
musubi-managed: true
promoted_from: 2W1eC...
promoted_at: 2026-04-16T04:00:02Z
created: 2026-04-16T04:00:02Z
updated: 2026-04-16T04:00:02Z
---

# CUDA 13 installation pattern

...
```

The promoted file carries the concept's namespace, because promotion copies `concept.namespace` into the curated frontmatter.

## Test Contract

**Module under test:** `src/musubi/vault/frontmatter.py`

Parsing:

1. `test_minimal_file_with_only_title_parses`
2. `test_fully_populated_file_parses`
3. `test_missing_title_errors`
4. `test_extra_fields_preserved_in_output`
5. `test_naive_datetime_rejected`
6. `test_importance_out_of_range_errors`
7. `test_invalid_ksuid_errors`

Normalization:

8. `test_tags_lowercased_on_write`
9. `test_datetime_serialized_with_z`

Authorization:

10. `test_musubi_managed_true_allows_system_write`
11. `test_musubi_managed_false_blocks_system_write`

Examples:

12. `test_example_minimal_file_equivalent_after_roundtrip`
13. `test_example_musubi_promoted_file_equivalent`

Skipped (round-trip formatting, aliases, identity-edit detection, integration):

14. `test_yaml_comments_preserved`
15. `test_key_order_preserved`
16. `test_quoted_string_style_preserved`
17. `test_tag_aliases_applied_on_write`
18. `test_musubi_managed_flag_flip_respected_next_promotion`
19. `test_bootstrap_object_id_writes_frontmatter_back` (covered by `tests/vault/test_sync.py::test_missing_object_id_gets_generated_and_written_back`)
20. `test_object_id_edit_by_human_logged_and_skipped`
21. `integration: create minimal file via editor simulation, watcher bootstraps object_id, file reread stable`
22. `integration: invalid frontmatter file → Thought emitted, no Qdrant change`
