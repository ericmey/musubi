---
title: "Slice: ART-002 — Artifact Purge Stub"
slice_id: slice-art002-purge-artifact
status: in-review
owner: shiori@home
phase: "Auth"
section: _slices
type: slice
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-16
reviewed: false
depends-on: []
blocks: []
---

# Slice: ART-002 — Artifact Purge Stub

Tracks #399.

## What

Make the `purge_artifact` endpoint truthful and functional. Replaces the 202 mock acknowledgment with a genuine hard delete of the artifact head, its committed chunks, and its blob file. Failures report truth rather than faking success, and the endpoint relies on Qdrant and the blob storage interfaces in an idempotent manner.

## Files
- `owns_paths`: 
  - `src/musubi/api/routers/writes_artifact.py`
  - `src/musubi/planes/artifact/plane.py`
  - `tests/api/test_api_v0_write.py`
  - `docs/Musubi/_slices/slice-art002-purge-artifact.md`

## Test Contract
1. `test_artifact_purge_truthful_and_idempotent`

## Work log
- Replaced the mocked 202 implementation in `purge_artifact`.
- Implemented `ArtifactPlane.purge()` extending to Qdrant chunks and head.
- Added strict exact tests mapping operator scopes directly against physical artifacts deleting exactly the blob path, `musubi_artifact`, and `musubi_artifact_chunks` without double-raises.
