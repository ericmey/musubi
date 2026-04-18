---
title: Data Model
section: 04-data-model
tags: [data-model, schema, section/data-model, status/complete, type/spec]
type: spec
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# 04 — Data Model

Schemas, relationships, lifecycle states. Every memory-like object is defined here.

## Documents in this section

- [[04-data-model/object-hierarchy]] — The inheritance-ish hierarchy of memory object types.
- [[04-data-model/episodic-memory]] — Schema + test contract for episodic memories.
- [[04-data-model/curated-knowledge]] — Schema + test contract for curated knowledge (markdown + Qdrant index).
- [[04-data-model/source-artifact]] — Schema + test contract for artifacts and their chunks.
- [[04-data-model/synthesized-concept]] — Schema + test contract for the bridge layer.
- [[04-data-model/thoughts]] — Schema for the preserved thoughts channel.
- [[04-data-model/lifecycle]] — State machine. Allowed transitions. Invariants.
- [[04-data-model/relationships]] — `derived_from`, `supersedes`, `merged_from`, `promoted_to`, `linked_to_topic`, `supported_by`, `contradicts`.
- [[04-data-model/temporal-model]] — Bitemporal fields and validity windows.
- [[04-data-model/qdrant-layout]] — Named vectors, payload indexes, collection layouts.
- [[04-data-model/vault-schema]] — Obsidian frontmatter schema for curated and artifact files.

## Principles

1. **One pydantic model per object type.** Models live in `musubi/types/`. No dict-shaped payloads floating around the codebase.
2. **Models are the schema.** We generate JSON Schema + OpenAPI from them. We do not maintain parallel schemas.
3. **Every model has `schema_version: int`.** Readers are forward-compatible; writers always write latest.
4. **No optional ID.** Object IDs (KSUID) are assigned at construction; they are never nullable on the wire.
5. **Timestamps are always ISO8601 UTC + epoch float.** Both, for human and query readability.
6. **Namespace is required, not defaulted.** See [[03-system-design/namespaces]].
7. **No silent mutation.** Every mutation produces a new version or lineage entry. See [[04-data-model/lifecycle]].

## Object type summary

| Type | Plane | Primary store | Derived index | Writer |
|---|---|---|---|---|
| `EpisodicMemory` | episodic | Qdrant | — | Adapters via API |
| `CuratedKnowledge` | curated | Obsidian vault (.md) | Qdrant (`musubi_curated`) | Human via Obsidian; Core via promotion |
| `SourceArtifact` | artifact | Object store (blob) | Qdrant chunks (`musubi_artifact_chunks`) | Adapters via API (POST /v1/artifacts) |
| `SynthesizedConcept` | concept | Qdrant (`musubi_concept`) | — | Lifecycle Worker |
| `Thought` | thought | Qdrant (`musubi_thought`) | — | Adapters via API |
| `LifecycleEvent` | — (audit) | sqlite (local) + Qdrant mirror | — | Core + Lifecycle Worker |

## Cross-cutting fields

Every object (regardless of plane) has:

```python
object_id: KSUID                   # 27-char sortable
namespace: str                     # "<tenant>/<presence>/<plane>"
schema_version: int                # current: 1
created_at: datetime               # UTC
created_epoch: float               # unix timestamp
updated_at: datetime
updated_epoch: float
version: int                       # 1 on create; increments on significant mutations
state: LifecycleState              # see lifecycle.md
```

Lifecycle-related fields are detailed in [[04-data-model/lifecycle]].

## Serialization

- **Wire format**: JSON on HTTP, protobuf on gRPC, both derived from pydantic.
- **Qdrant payload format**: pydantic `.model_dump(mode="json")` — all datetimes as ISO8601, enums as strings, lists as lists.
- **Markdown frontmatter**: a subset of the model serialized as YAML. See [[04-data-model/vault-schema]].

## Validation invariants

Enforced by pydantic + explicit `model_validator`:

- `created_epoch` ≈ `datetime_to_epoch(created_at)` (within 1s).
- `updated_epoch ≥ created_epoch`.
- `version ≥ 1`.
- `namespace` matches regex `^[a-z0-9-]+/[a-z0-9-_]+/[a-z]+$`.
- Plane-specific: see individual specs.

## Migration rules

When evolving a schema:

1. **Additive only** for minor versions. New optional field, new optional enum value, new optional relationship.
2. **Breaking** bumps `schema_version`. Readers must handle all prior versions (forward-compatible). Migration job re-writes old objects to the new version lazily (on next access) or in a background sweep.
3. **Never rename fields in-place.** Add new field, deprecate old, leave both for one minor version, remove.
4. **Renames in the vault frontmatter** are handled the same way — but via a one-time migration script committed as an ADR, because human files are involved.

See [[11-migration/schema-evolution]].
