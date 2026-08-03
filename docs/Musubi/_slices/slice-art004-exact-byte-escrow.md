---
title: "Slice: ART-004 exact-byte retraction escrow"
slice_id: slice-art004-exact-byte-escrow
issue: 644
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "4-planes"
tags: [section/slices, status/in-progress, type/slice, artifacts, idempotency, data-integrity]
updated: 2026-08-03
reviewed: false
depends-on: [slice-art003-stored-unindexed]
blocks: []
---

# Slice: ART-004 exact-byte retraction escrow

Implement ADR 0042's durable midpoint: exact original bytes become a
deterministically addressed, stored-unindexed artifact before any later episodic
mutation is permitted.

## Decision boundary

- The deterministic address is derived exactly from the accepted ADR formula
  and retains the source episodic KSUID timestamp bytes.
- Blob publication is durable, atomic, and no-clobber. Existing bytes are
  reusable only after exact SHA-256 and byte-length readback.
- No artifact head is externally readable until the final blob verifies.
- A newly published escrow head starts at `publication_version=0` and is
  write-once. Existing exact blob/head state is adopted rather than republished.
- Escrow publishes no chunks and admits no indexing intent. The surviving
  synchronous `ArtifactPlane.index()` door must typed-refuse
  `stored_unindexed` rather than making it searchable.
- Automatic retention must not purge `stored_unindexed` artifacts. Explicit
  operator hard purge remains the only corrupt-address recovery and is never
  invoked by this primitive.
- Ordinary artifact upload/indexing remains unchanged.
- Episodic tombstone mutation, retraction evidence, endpoint authorization, and
  fleet consumer cutover remain owned by #645, #646, and fleet-tools #32.

## Specs to implement

- [[04-data-model/source-artifact]]
- [[13-decisions/0042-escrow-backed-episodic-retraction]]

## Owned paths

- `src/musubi/planes/artifact/escrow.py`
- `src/musubi/planes/artifact/__init__.py`
- `src/musubi/planes/artifact/plane.py`
- `src/musubi/ops/retention.py`
- `tests/planes/test_artifact_escrow.py`
- `tests/integration/test_artifact_escrow.py`
- `tests/ops/test_retention.py`
- `docs/Musubi/04-data-model/source-artifact.md`
- `docs/Musubi/_slices/slice-art004-exact-byte-escrow.md`
- `docs/Musubi/_inbox/locks/slice-art004-exact-byte-escrow.lock`

## Test contract

1. The accepted ADR formula has a frozen deterministic-id golden vector.
2. Namespace, source object id, or original digest changes the escrow address;
   its timestamp bytes remain the source object's.
3. Blob write/readback failure exposes no artifact head.
4. Head-publication failure followed by retry reuses only exact verified bytes
   and publishes one version-zero head.
5. A pre-existing truncated or divergent final blob fails closed and is never
   overwritten.
6. Concurrent identical attempts converge on one exact blob and one identical
   artifact head.
7. Same operation identity with a different original digest conflicts rather
   than publishing a second escrow.
8. A verified escrow is exactly readable by id, has zero chunks, and creates no
   indexing intent.
9. Exact-text semantic search misses the escrow while the same query finds a
   normally indexed positive-control artifact.
10. `ArtifactPlane.index()` refuses stored-unindexed heads without writing chunks
    or changing `publication_version`.
11. Automatic artifact retention skips stored-unindexed heads even when an
    artifact TTL is configured.
12. Real filesystem plus real-Qdrant integration proves bytes-before-head,
    exact readback, and deterministic retry ordering.

## Work log

- 2026-08-03 — Claimed #644 after ART-003 merged as #649 / `58309c4`.
  This lane inherits the two boundaries deliberately left by ART-003:
  `publication_version=0`/write-once and the ungated legacy
  `ArtifactPlane.index()` door.

