---
title: "Agent Rules — Data Model (04)"
section: 04-data-model
type: index
status: complete
tags: [section/data-model, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: true
---

# Agent Rules — Data Model (04)

Local rules for any change to `src/musubi/types/`, `src/musubi/store/specs.py`, or `src/musubi/planes/**`. Supplements [[CLAUDE]] and [[00-index/conventions]].

## Must

- **Pydantic v2 for every data shape.** No TypedDicts, no dataclasses for payloads. Pydantic models only.
- **Named vectors from day one.** Even a single-model collection declares `vectors={"dense_<model>_<version>": ...}`. Never unnamed.
- **Validity fields on every memory object:** `valid_from`, `valid_until` (nullable, on `MemoryObject`, `src/musubi/types/base.py:154-157`). Episodic memories also carry `event_at` and `ingested_at` (`src/musubi/types/episodic.py:108-109`). See [[04-data-model/temporal-model]].
- **Lineage fields on every mutable object:** `supersedes`, `superseded_by`, `merged_from`, `version`, `state`. See [[04-data-model/lifecycle]].
- **KSUID object ids.** Qdrant point-id stays UUID; KSUID lives in payload as `object_id`. See [[00-index/conventions#IDs]].
- **Schema version on every payload.** `schema_version: int`, forward-readable. Writer always writes latest.

## Must not

- Use `datetime.now()` without `tz=UTC`.
- Cite artifact evidence as a bare id. Use `ArtifactRef` (`artifact_id`, optional `chunk_id`, optional `quote`; `src/musubi/types/common.py:140-150`) in `supported_by`.
- Change a memory without bumping `version`, or change its `state` without a `LifecycleEvent`. Replacing a memory's meaning uses `supersedes` / `superseded_by` pointing at the old one.
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
