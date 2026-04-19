---
title: "Slice: Curated knowledge plane"
slice_id: slice-plane-curated
section: _slices
type: slice
status: in-review
owner: vscode-cc-opus47
phase: "4 Planes"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]"]
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-lifecycle-reflection]]", "[[_slices/slice-retrieval-blended]]", "[[_slices/slice-vault-sync]]"]
---
# Slice: Curated knowledge plane

> Topic-first durable facts. Obsidian vault is the store of record; Qdrant is a derived index rebuilt from the vault.

**Phase:** 4 Planes · **Status:** `in-review` · **Owner:** `vscode-cc-opus47`

## Specs to implement

- [[04-data-model/curated-knowledge]]

## Owned paths (you MAY write here)

- `musubi/planes/curated/`
- `tests/planes/test_curated.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/episodic/`
- `musubi/planes/artifact/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-vault-sync]]
- [[_slices/slice-retrieval-blended]]

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

### 2026-04-19 — vscode-cc-opus47 — claim

- Claimed slice atomically via `gh issue edit 22 --add-assignee @me`. Issue #22, PR #39 (draft).
- Branch `slice/slice-plane-curated` off `v2`.

### 2026-04-19 — vscode-cc-opus47 — handoff to in-review

- Landed `src/musubi/planes/curated/{__init__,plane}.py`: `CuratedPlane` with vault-path-keyed dedup (idempotent on same `body_hash`, supersession on different `body_hash`), namespace-isolated `get`/`query`, bitemporal default predicate, and curated-table `transition()` emitting `LifecycleEvent`.
- Tests: 14 passing + 15 skipped-with-reason in `tests/planes/test_curated.py`. Coverage 96 % branch on `src/musubi/planes/curated/` (gate is 90 %). `make check` clean: ruff format + lint + mypy strict + pytest. `make agent-check` clean (warnings only — pre-existing in repo, none from this slice). `make tc-coverage SLICE=slice-plane-curated` exits 0.
- PR #39 marked ready for review.

#### Test Contract coverage matrix

| # | Bullet | State | Where |
|---|---|---|---|
| 1 | `test_read_from_qdrant_returns_indexed_fields` | ✓ passing | `tests/planes/test_curated.py` |
| 2 | `test_read_with_include_body_reads_from_vault_filesystem` | ⏭ skipped | deferred → slice-vault-sync (`src/musubi/vault_sync/`) |
| 3 | `test_human_edit_triggers_reindex_after_debounce` | ⏭ skipped | deferred → slice-vault-sync |
| 4 | `test_reindex_updates_body_hash_and_version` | ⏭ skipped | deferred → slice-vault-sync |
| 5 | `test_identical_content_save_no_index_write` | ✓ passing | `tests/planes/test_curated.py` |
| 6 | `test_file_move_updates_vault_path_in_qdrant` | ⏭ skipped | deferred → slice-vault-sync |
| 7 | `test_file_delete_archives_and_marks_state` | ⏭ skipped | deferred → slice-vault-sync |
| 8 | `test_frontmatter_missing_object_id_gets_generated_and_written_back` | ⏭ skipped | deferred → slice-vault-sync |
| 9 | `test_frontmatter_schema_invalid_file_is_not_indexed_and_emits_thought` | ⏭ skipped | deferred → slice-vault-sync |
| 10 | `test_musubi_managed_true_file_accepts_system_write` | ⏭ skipped | deferred → slice-vault-sync (`vault_sync/writer.py`) |
| 11 | `test_musubi_managed_false_file_rejects_system_write` | ⏭ skipped | deferred → slice-vault-sync |
| 12 | `test_write_log_echo_detection_prevents_double_index` | ⏭ skipped | deferred → slice-vault-sync |
| 13 | `test_promotion_writes_file_and_index_atomically_enough` | ⏭ skipped | deferred → slice-lifecycle-promotion (`src/musubi/lifecycle/`) |
| 14 | `test_promotion_links_concept_to_curated_via_promoted_to_and_promoted_from` | ⏭ skipped | deferred → slice-lifecycle-promotion |
| 15 | `test_large_file_chunks_body_as_artifact_and_references` | ⏭ skipped | deferred → slice-plane-artifact |
| 16 | `test_bitemporal_valid_until_excludes_from_default_query` | ✓ passing | `tests/planes/test_curated.py` |
| 17 | `test_supersession_chain_read_returns_latest` | ✓ passing | `tests/planes/test_curated.py` |
| 18 | `test_cross_namespace_reference_logged_in_audit` | ⏭ skipped | deferred → slice-lifecycle-engine |
| 19 | `test_isolation_read_enforcement` | ✓ passing | `tests/planes/test_curated.py` |
| 20 | `test_hard_delete_requires_operator_scope` | ⏭ skipped | deferred → slice-auth |
| 21 | `hypothesis: vault_path <-> object_id is a bijection for non-archived files` | ⊘ out-of-scope | property test — deferred to a follow-up `test-property-curated` slice; the bijection is an emergent property of the vault watcher's uniqueness enforcement on `(namespace, vault_path)`, which lives in slice-vault-sync. The plane preserves it in isolation but cannot prove the bijection without the watcher. |
| 22 | `hypothesis: body_hash changes iff content bytes change (ignoring frontmatter)` | ⊘ out-of-scope | property of the vault watcher's body-hash computation, not the plane. Deferred to slice-vault-sync. |
| 23 | `integration: rebuild_curated_from_vault matches live state within 1%` | ⊘ out-of-scope | requires `rebuild_curated_from_vault` rebuilder + a real vault on disk; deferred to slice-vault-sync. |
| 24 | `integration: concurrent human edit + promotion write to same path produces a deterministic winner` | ⊘ out-of-scope | requires both the watcher (slice-vault-sync) and the promotion worker (slice-lifecycle-promotion). Deferred to those slices. |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- #39 — `feat(planes): slice-plane-curated` (in-review)
