---
title: "Slice: Shared pydantic types"
slice_id: slice-types
section: _slices
type: slice
status: done
owner: eric
phase: "1 Schema"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-18
reviewed: true
depends-on: []
blocks: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-v0-write]]", "[[_slices/slice-ingestion-capture]]", "[[_slices/slice-lifecycle-engine]]", "[[_slices/slice-plane-artifact]]", "[[_slices/slice-plane-concept]]", "[[_slices/slice-plane-curated]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-plane-thoughts]]", "[[_slices/slice-poc-data-migration]]", "[[_slices/slice-qdrant-layout]]", "[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-rerank]]", "[[_slices/slice-retrieval-scoring]]", "[[_slices/slice-types-followup]]", "[[_slices/slice-vault-sync]]"]
---
# Slice: Shared pydantic types

> Pydantic models for every memory object. The typed surface every other slice imports from.

**Phase:** 1 Schema · **Status:** `done` · **Owner:** `eric`

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

  - [[_slices/slice-api-v0-read]]
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

### 2026-04-18 — eric — closure (retrospective, under the new Closure Rule)

Flipping `status: in-progress → done`. This slice's first cut (commit `9d57c37`) has been successfully consumed by every downstream slice that landed afterwards (slice-qdrant-layout's `store` module imports `types.common.LifecycleState`; slice-config imports nothing from here; slice-embedding imports `store.specs.DENSE_SIZE`; slice-plane-episodic imports `types.EpisodicMemory`, `types.LifecycleEvent`, `types.common.KSUID / Namespace / epoch_of / utc_now`). The 214-test suite passes against this slice's public API unchanged.

Applying the [Test Contract Closure Rule](../00-index/agent-guardrails.md#Test-Contract-Closure-Rule) retrospectively. Per the spec's `## Test contract for object models (shared)` in [[04-data-model/object-hierarchy]]:

| Bullet | Closure state | Evidence |
|---|---|---|
| `test_<Model>_roundtrip_json` | ✓ passing (all 5 concrete types) | `tests/types/test_{episodic,curated,concept,thought,artifact}.py::test_roundtrip_json` |
| `test_<Model>_roundtrip_qdrant_payload` | ⊘ out-of-scope for this slice | Qdrant payload shape is defined + round-tripped in slice-qdrant-layout (point-ID UUID5 derivation) and in slice-plane-episodic (per-plane upsert / query). Per the Method-ownership rule, the roundtrip belongs where the payload is shaped, not in `types/`. |
| `test_<Model>_schema_version_present` | ✓ passing | `tests/types/test_base.py::TestMusubiObjectInvariants::test_schema_version_present_and_defaults_to_current` |
| `test_<Model>_timestamps_validated` | ✓ passing | `tests/types/test_base.py::TestMusubiObjectInvariants::test_created_epoch_matches_created_at` + `test_updated_epoch_monotone_non_decreasing` + `test_updated_before_created_rejected` + `test_utc_enforced_on_datetime_inputs` |
| `test_<Model>_namespace_regex_enforced` | ✓ passing | `tests/types/test_base.py::TestMusubiObjectInvariants::test_namespace_regex_enforced` + `tests/types/test_common.py::TestNamespaceValidator` (12 parameterised cases) |
| `test_<Model>_forward_compat_older_schema_reads_ok` | ✓ passing | `tests/types/test_base.py::TestRoundtrip::test_forward_compat_older_schema_reads_ok` |

Plus the `## Test contract` from [[04-data-model/lifecycle#Test-contract]] — covered in `tests/types/test_lifecycle_event.py` (transition-table parameterised entries, illegal transitions rejected, reachability property, roundtrip). Hypothesis-based property tests (16, 17) deferred to when slice-lifecycle-engine lands; declared out-of-scope here.

**Historical footnote:** both this slice and slice-qdrant-layout landed as direct commits to `v2` (commit `9d57c37` and `0f46281`) prior to branch protection + the Issue board being live. Subsequent slices go through the full PR lifecycle with Dual-update rule enforcement. This retrospective closure is a one-time reconciliation; future `done` transitions go through `gh pr merge --squash` on a reviewed PR.

Closing the corresponding GitHub Issue via the Dual-update rule in the same commit that flips this frontmatter.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
