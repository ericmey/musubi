---
title: "Slice: storage-aware row integrity sweep"
slice_id: slice-val002-storage-aware-sweep
issue: 625
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, validation, data-integrity]
updated: 2026-08-02
reviewed: false
depends-on: []
blocks: []
---

# Slice: storage-aware row integrity sweep

Restore the production row-integrity gate after DATA-001 Phase 2 introduced
legitimate physical anchor/content shapes that the older raw-domain validator
misclassified as corruption. Keep the public domain models strict and make the
sweep validate both physical storage integrity and resolved logical integrity.

## Decision boundary

- Do not add storage fields to domain models or relax `extra="forbid"`.
- Validate legacy identity, anchor, and content rows as distinct strict shapes.
- Validate each anchor's forward pointer, identity match, and commit-generation
  binding, then validate the resolved logical object with its existing domain model.
- Report unreferenced content explicitly and separately from corruption. Content
  points are immutable history, so an unreferenced point may be a superseded
  generation or crash-staged work; raw shape alone cannot distinguish the two.
  Preserve the reverse-reachability inventory without making either inference.
- Preserve the original unknown-key injection detector on every physical shape.

## Owned paths

- `src/musubi/cli/validate.py`
- `tests/cli/test_validate.py`
- `docs/Musubi/_slices/slice-val002-storage-aware-sweep.md`
- `docs/Musubi/_inbox/locks/slice-val002-storage-aware-sweep.lock`

## Test Contract

- `test_valid_legacy_identity_row_passes`
- `test_valid_anchor_and_content_rows_pass_with_pointer_binding`
- `test_injected_key_is_rejected_for_each_physical_shape`
- `test_dangling_cross_object_and_stale_generation_pointers_are_rejected`
- `test_resolved_logical_object_still_uses_strict_domain_model`
- `test_unreferenced_content_is_reported_without_being_called_corrupt`
- `test_full_data001_fixture_returns_clean`

## Work log

- 2026-08-02 — `codex-gpt5` claimed Issue #625 after the mandatory pre-v1.19
  sweep returned 49 false positives across 12,189 physical rows. A read-only
  controlled verifier proved they are 33 valid logical objects plus 16 intentional
  curated content snapshots; Aoi independently challenged the pointer-generation
  seam and accepted the no-waiver boundary. Production deployment remains held.
- 2026-08-02 — Added strict legacy, anchor, episodic-content, and curated-content
  physical validators without changing the domain models. The sweep now checks
  anchor target existence, namespace/object identity, commit-generation binding,
  and the resolved logical domain object. Unreferenced immutable content is
  separately enumerable in JSON and human output without being called corrupt.
  An interrupted multi-page scan validates the physical rows it saw but withholds
  cross-row pointer conclusions whose targets may be on unseen pages. Focused
  contract: 29 passed. Full `make check`: 2,480 passed, 195 skipped, 136
  deselected, 3 expected xfails; 88.31% coverage; Ruff and mypy passed.
