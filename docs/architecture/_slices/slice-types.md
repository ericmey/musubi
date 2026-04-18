---
title: "Slice: Shared pydantic types"
slice_id: slice-types
section: _slices
type: slice
status: in-progress
owner: eric
phase: "1 Schema"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: []
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-lifecycle-engine]]", "[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-thoughts]]", "[[_slices/slice-qdrant-layout]]", "[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-rerank]]", "[[_slices/slice-retrieval-scoring]]", "[[_slices/slice-vault-sync]]"]
---
# Slice: Shared pydantic types

> Pydantic models for every memory object. The typed surface every other slice imports from.

**Phase:** 1 Schema · **Status:** `in-progress` · **Owner:** `eric`

## Specs to implement

- [[04-data-model/object-hierarchy]]
- [[04-data-model/lifecycle]]
- [[04-data-model/temporal-model]]

## Owned paths (you MAY write here)

  - `musubi/types/`
  - `musubi/schema/`
  - `musubi/models.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/api/`
  - `musubi/retrieve/`
  - `musubi/lifecycle/`
  - `musubi/planes/`

## Depends on

  - _(no upstream slices)_

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-api-v0]]
  - [[_slices/slice-plane-episodic]]
  - [[_slices/slice-plane-curated]]
  - [[_slices/slice-plane-artifact]]
  - [[_slices/slice-plane-concept]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-17 — eric — first cut landed on `v2` branch

- Scaffolded ``src/musubi/types/`` under the monorepo layout (ADR 0015): `common`, `base`, `episodic`, `curated`, `concept`, `artifact`, `thought`, `lifecycle_event`. Package `__init__` re-exports the public surface.
- Implemented `MusubiObject` + `MemoryObject` bases with bitemporal fields (`valid_from`, `valid_until`, + `_epoch` mirrors), lineage (`supersedes`, `superseded_by`, `merged_from`, `linked_to_topics`, `supported_by`, `contradicts`, `derived_from`), and monotonicity invariants (`updated_epoch >= created_epoch`, `version >= 1`).
- Implemented all five concrete types per [[04-data-model/object-hierarchy]] with per-type state narrowing (`EpisodicMemory` ↦ provisional/matured/demoted/archived/superseded; `CuratedKnowledge` ↦ matured/superseded/archived; `SynthesizedConcept` ↦ synthesized/matured/promoted/demoted/superseded; `Thought` ↦ provisional/matured/archived; `SourceArtifact` ↦ matured/archived/superseded with orthogonal `artifact_state` axis of indexing/indexed/failed). Plus `ArtifactChunk` (frozen, offsets ordered) and `ArtifactRef` (frozen).
- Added `LifecycleEvent` + allowed-transition table (`is_legal_transition`, `legal_next_states`, `allowed_states`) sourced from [[04-data-model/lifecycle#Allowed transitions per type]]. Illegal transitions fail at event-construction time, so the engine can trust the validator.
- `Result[T, E]` implemented as `Ok[T] | Err[E]` with PEP 695 type params, frozen, discriminated on `kind`.
- KSUID dependency swapped from `ksuid>=1.2` (40-char hex) to `svix-ksuid>=0.6` (the 27-char base62 form the vault mandates).
- Validators: `Namespace` regex (`tenant/presence/plane`), KSUID regex, UTC-only datetimes, `valid_from <= valid_until`, self-supersession rejected, body-hash/sha256 = 64-char hex, promotion-metadata pair invariants on concept + curated.
- Tests: 8 test modules under `tests/types/`, 110 tests total covering the test contract from both `object-hierarchy.md` (roundtrip_json, schema_version, timestamps, namespace_regex, forward-compat) and `lifecycle.md` (transition table entries parameterised, illegal transitions rejected, reachability). Qdrant-payload roundtrip deferred to slice-qdrant-layout.
- `make check` clean: ruff format + lint + `mypy --strict` on 23 files + pytest 110 passing.
- Commit on `v2`: see PR links below (forthcoming).

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
