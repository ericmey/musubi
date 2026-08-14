---
title: "Slice: DATA-001 physical layout-field leak"
slice_id: slice-data001-layout-leak-697
issue: 697
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-review, type/slice, data-integrity]
updated: 2026-08-14
reviewed: false
depends-on: [slice-data001-phase2-immutable-vectors]
blocks: []
---

# Slice: DATA-001 physical layout-field leak

The production pre-deploy integrity sweep found a v2 episodic anchor carrying
content-only `generation` and `owner_token` fields. The logical resolver merges
the immutable content snapshot with the mutable anchor, but the publisher fed
that merged storage envelope back into `_rebase()`. A payload-only reinforcement
could therefore copy content-layout fields onto the anchor while still returning
a healthy logical object.

## Decision boundary

- Keep `resolve_committed_content()` as the authoritative logical read surface.
- Strip layout-only fields before the resolved object enters rebase or embedding
  projection comparison; retain the unstripped read only for replay and version
  fencing.
- Strip the rebased result again at the durable writer boundary so a malformed
  or legacy descriptor cannot reintroduce layout fields through `set_fields` or
  `new_memory`, even if an upstream validator regresses.
- If stripping leaves no domain fields, treat the object as needing a full
  publish rather than preserving a layout-only shell through a payload-only
  mutation. This is an intentional fail-safe behavior change.
- Do not relax the strict physical validators or domain models.
- Do not infer that this defect caused the separate mutable-domain metadata found
  on the production content point; that path is not established by current code.

## Owned paths

- `src/musubi/store/immutable_vectors.py`
- `tests/store/test_data001_phase2_immutable_vectors.py`
- `tests/store/test_data001_layout_field_leak.py`
- `docs/Musubi/_slices/slice-data001-layout-leak-697.md`
- `CHANGELOG.md`

## Specs to implement

- [[_slices/slice-data001-layout-leak-697]] — this slice's physical writer
  boundary and `## Test Contract` below.

## Test Contract

- `test_curated_projection_decides_vector_change`
- `test_episodic_reinforce_with_summary_is_projection_based`
- `test_payload_only_rebase_strips_layout_fields_from_anchor`
- `test_vector_change_rebase_preserves_strict_physical_envelopes`

## Work log

- 2026-08-14 — `codex-gpt5` reproduced Issue #697 against the real publisher
  with in-memory Qdrant. The payload-only episodic test failed because the raw
  anchor contained content `generation` and `owner_token`, even though the
  resolved logical object remained valid. Deployment of v1.24.0 remains held.
- 2026-08-14 — Stripped the resolved storage envelope to domain state before
  rebase and projection comparison while retaining the raw read for exact
  operation replay and version fencing. Added default-gate physical-envelope
  regressions alongside the original Phase 2 discriminators. Test Contract:
  4/4 passing. `make check`: 2,668 passed, 195 skipped, 140 deselected, 2
  expected xfails; 88.59% coverage; Ruff and strict mypy passed. `make
  tc-coverage SLICE=slice-data001-layout-leak-697` and `make agent-check` both
  passed (agent-check warnings only). Production repair and deployment remain
  separately held on snapshot, explicit disposition, and a clean full sweep.
