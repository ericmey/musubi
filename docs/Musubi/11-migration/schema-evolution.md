---
title: Schema Evolution
section: 11-migration
tags: [migration, pydantic, schema, section/migration, status/complete, type/migration-phase, versioning]
type: migration-phase
status: complete
updated: 2026-04-17
up: "[[11-migration/index]]"
reviewed: false
---
# Schema Evolution

How pydantic schemas + Qdrant payloads + vault frontmatter change over time without forcing re-migration or breaking existing clients.

## Three schemas to keep in sync

1. **API request/response** — pydantic models served via OpenAPI.
2. **Qdrant payloads** — what we store on each point.
3. **Vault frontmatter** — what humans write.

All derive from shared pydantic definitions in `musubi/models.py`. This eliminates drift: change one place, regenerate downstream.

## Versioning policy

### Additive changes (non-breaking)

- Add optional fields.
- Add enum values (where consumers are tolerant).
- Add new endpoints / routes.

Stays in the same API version. No migration needed for existing data (new fields default to null/empty).

### Breaking changes

- Remove or rename fields.
- Change field type.
- Tighten enum (remove a value).
- Change endpoint signature.

Forces:

- New API major version (`/v2/...`) running side-by-side for 180 days with `/v1/...`.
- Qdrant payload migration job.
- Vault frontmatter auto-update (where unambiguous) or operator intervention (where ambiguous).

## Every payload has `schema_version`

```python
class BasePayload(BaseModel):
    schema_version: int = 1
```

When we change the schema, bump to `2`. The service handles both in reads; writes always use the latest.

```python
def parse_payload(raw: dict) -> Payload:
    v = raw.get("schema_version", 1)
    if v == 1:
        return upgrade_v1_to_v2(PayloadV1.model_validate(raw))
    return PayloadV2.model_validate(raw)
```

## Migration jobs

Add a lifecycle job: `schema_migration_<from>_<to>`. Scheduled once; removed after completion.

Example v1 → v2 migration:

1. Scroll all points with `schema_version = 1`.
2. Transform payload.
3. Write back with `schema_version = 2`.
4. Track cursor for resume.

On completion, remove the `schema_version == 1` handling from parse.

## Vault frontmatter

Vault frontmatter evolves alongside `CuratedFrontmatter`. For new fields:

- Schema is `extra="allow"` (see [[06-ingestion/vault-frontmatter-schema]]) — humans can add custom fields without validation failing.
- Required new fields prompt the promotion template to include them on future writes.
- Missing new fields on existing docs: validated in "lax" mode (warning, not error) until a sweep upgrades them.

### Auto-upgrade sweep

A script can touch every curated file, add missing fields with sensible defaults, commit to git with a clear message. Operator-triggered, not automatic (humans should review bulk changes).

## OpenAPI generation

```
# CI step
python -m musubi.tooling.gen_openapi > api/openapi.yaml
```

Commits the generated file on every release. The file becomes part of the tagged version. Adapters lock against a specific schema.

## Backward compatibility testing

In CI:

1. Deploy v1 API.
2. Run v1 contract suite. Pass.
3. Deploy v2 API (adds fields, doesn't remove).
4. Run v1 contract suite against v2. Must pass.
5. Run v2 contract suite against v2. Must pass.

If v2 breaks v1, the change is breaking — needs full migration.

## Cross-presence compat

Adapters pin SDK version, SDK pins minimum Core API version. Matrix:

| SDK version | Min Core | Max Core |
|---|---|---|
| 1.0 | 1.0 | 1.x |
| 1.2 | 1.2 | 1.x |
| 2.0 | 2.0 | 2.x |

Core supports both 1.x and 2.x simultaneously for 180 days (path-prefixed).

## Deprecation cycle

A field we want to remove:

1. Mark deprecated in pydantic docstring.
2. Remove from the render template (new writes don't include it).
3. Migration job clears the field on existing rows.
4. Remove from the model (next major version).

## Test contract

**Module under test:** `musubi/models.py` + migration infra

1. `test_payload_parse_accepts_schema_v1_and_v2`
2. `test_schema_migration_job_converges`
3. `test_openapi_regenerates_without_diff_on_idempotent_run`
4. `test_backward_compat_v1_against_v2_server`
5. `test_vault_frontmatter_extra_fields_preserved`
6. `test_missing_new_required_field_warns_not_errors`
