---
title: Agent Rules — Data Model
section: 04-data-model
type: index
status: complete
tags: [section/data-model, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: true
---

# Agent Rules — Data Model (04)

Local rules for any slice writing to `musubi/types/`, `musubi/schema/`, `musubi/models.py`, or `musubi/planes/**`. Supplements [[CLAUDE]] and [[00-index/conventions]].

## Must

- **Pydantic v2 for every data shape.** No TypedDicts, no dataclasses for payloads. Pydantic models only.
- **Named vectors from day one.** Even a single-model collection declares `vectors={"dense_<model>_<version>": ...}`. Never unnamed.
- **Bitemporal fields on every memory object:** `event_at`, `ingested_at`, `valid_from`, `valid_until` (the last two nullable). See [[04-data-model/temporal-model]].
- **Lineage fields on every mutable object:** `supersedes`, `superseded_by`, `merged_from`, `version`, `state`. See [[04-data-model/lifecycle]].
- **KSUID object ids.** Qdrant point-id stays UUID; KSUID lives in payload as `object_id`. See [[00-index/conventions#IDs]].
- **Schema version on every payload.** `schema_version: int`, forward-readable. Writer always writes latest.

## Must not

- Use `datetime.now()` without `tz=UTC`.
- Store plane-crossing references as raw ids. Use the `{plane}/{object_id}` citation form.
- Mutate a memory in place. Mutations create a new version with `supersedes` pointing at the old.
- Introduce a new top-level field without bumping `schema_version` and updating the reader.

## Plane truth models (don't conflate)

| Plane    | Truth                                         | Primary store         | Retention                   |
|----------|-----------------------------------------------|-----------------------|-----------------------------|
| Episodic | high-recall, high-noise, source-first         | Qdrant                | TTL-bound provisional + matured indefinite |
| Curated  | human-authored, low-noise, stable             | Obsidian vault (SoR)  | Indefinite                  |
| Artifact | ground truth, never mutated, additive         | blob store + Qdrant chunks | Indefinite               |
| Concept  | machine-generated bridge between episodic↔curated | Qdrant            | Until promoted or demoted   |

## When to add a new object type

Don't unless you've exhausted:

- Extending an existing object via a new optional field (bump `schema_version`).
- Adding a lineage relationship (see [[04-data-model/relationships]]).

New object types require an ADR in [[13-decisions/index]].

## Test Contract conventions

When adding a `## Test Contract` section to a spec, use `- [ ]` checkboxes. One per behaviour. The test file names map 1-to-1.

```markdown
## Test Contract

- [ ] `episodic_store` rejects empty content with `Err(ValidationError)`.
- [ ] `episodic_store` writes `state=provisional` on first insert.
- [ ] `episodic_store` reinforces (does not duplicate) at similarity ≥ 0.92.
```

## Related slices

- [[_slices/slice-types]] — the only slice that writes `musubi/types/`, `musubi/models.py`.
- [[_slices/slice-plane-episodic]], [[_slices/slice-plane-curated]], [[_slices/slice-plane-artifact]], [[_slices/slice-plane-concept]], [[_slices/slice-plane-thoughts]] — plane-specific slices.
- [[_slices/slice-qdrant-layout]] — collection bootstrap.
