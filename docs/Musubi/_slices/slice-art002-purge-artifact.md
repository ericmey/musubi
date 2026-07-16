---
title: "Slice: ART-002 — Artifact Purge Stub"
slice_id: slice-art002-purge-artifact
status: done
owner: shiori@home
phase: "Auth"
section: _slices
type: slice
tags: [section/slices, status/done, type/slice]
updated: 2026-07-16
reviewed: true
depends-on: []
blocks: []
---

# Slice: ART-002 — Artifact Purge Stub

Tracks #399.

## What

Make the `purge_artifact` endpoint truthful and functional. Replaces the 202 mock acknowledgment with a genuine hard delete of the artifact head, its committed chunks, and its blob file. Failures report truth rather than faking success, and the endpoint relies on Qdrant and the blob storage interfaces in an idempotent manner. Uses head-first purge ordering to fence any already-running `ArtifactIndexer` publish (the head readback/filter vanishes).

## Specs to implement
- [[04-data-model/source-artifact]]

## Files
- `owns_paths`:
  - `src/musubi/api/routers/writes_artifact.py`
  - `src/musubi/planes/artifact/plane.py`
  - `tests/api/test_api_v0_write.py`
  - `docs/Musubi/_slices/slice-art002-purge-artifact.md`

## Test Contract
1. `test_artifact_purge_truthful_and_idempotent_and_fenced`

## Work log
- Replaced the mocked 202 implementation in `purge_artifact`.
- Implemented `ArtifactPlane.purge()` extending to Qdrant chunks and head. Uses `wait=True` on Qdrant operations to guarantee durable completion.
- Adopted head-first purge ordering (deleting head fences any in-flight intent handlers trying to publish chunks).
- Added strict exact tests mapping operator scopes directly against physical artifacts deleting exactly the blob path, `musubi_artifact`, and `musubi_artifact_chunks` without double-raises.
- Built a fresh, C4-compatible test that creates a canonical blob and intent, reconciles the `ArtifactIndexer`, verifies the physical chunks and the head's committed generation/owner are deleted correctly, ensures idempotency, and confirms a resurrected indexing intent fails (fences) safely on the absent head.

## Definition of Done
- Endpoint `POST /v1/artifacts/{id}/purge` successfully unlinks the blob and Qdrant head/chunks.
- Head is deleted first to fence `ArtifactIndexer`.
- Idempotency is preserved on retry.
- Test discriminator proves the load-bearing no-resurrection invariant (re-enqueued intent dies).
