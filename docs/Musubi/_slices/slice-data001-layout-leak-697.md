---
title: "Slice: DATA-001 physical layout-field leak"
slice_id: slice-data001-layout-leak-697
issue: 697
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, data-integrity]
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
- Do not relax the strict physical validators or domain models.
- Do not infer that this defect caused the separate mutable-domain metadata found
  on the production content point; that path is not established by current code.

## Owned paths

- `src/musubi/store/immutable_vectors.py`
- `tests/store/test_data001_phase2_immutable_vectors.py`
- `docs/Musubi/_slices/slice-data001-layout-leak-697.md`
- `CHANGELOG.md`

## Test Contract

- `test_curated_projection_decides_vector_change`
- `test_episodic_reinforce_with_summary_is_projection_based`

## Work log

- 2026-08-14 — `codex-gpt5` reproduced Issue #697 against the real publisher
  with in-memory Qdrant. The payload-only episodic test failed because the raw
  anchor contained content `generation` and `owner_token`, even though the
  resolved logical object remained valid. Deployment of v1.24.0 remains held.

