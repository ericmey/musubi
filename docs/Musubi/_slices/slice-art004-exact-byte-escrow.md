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
- The retention proof invokes a configured future artifact policy directly;
  there is no artifact TTL or live retained population today, so it proves the
  guard rather than claiming production retention activity.
- Ordinary artifact upload/indexing remains unchanged. Its existing
  `write_bytes` path remains less durable by design: escrow is the only blob
  whose loss is unrecoverable. ART-004's temp+fsync+hard-link protocol is not a
  silent rewrite of ordinary upload.
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
3. A temp-file fsync failure exposes neither a final blob nor an artifact head;
   a final-blob readback failure exposes no artifact head.
4. Head-publication failure followed by retry reuses only exact verified bytes
   and publishes one version-zero head.
5. A pre-existing truncated or divergent final blob fails closed and is never
   overwritten.
6. Concurrent identical attempts converge on one exact blob and one identical
   artifact head.
7. A verified escrow is exactly readable by id, has zero chunks, and creates no
   indexing intent.
8. Exact-text semantic search misses the escrow while the same query finds a
   normally indexed positive-control artifact.
9. `ArtifactPlane.index()` refuses a live stored-unindexed head even when the
   caller holds a stale indexing-state object, without writing chunks or
   changing `publication_version`.
10. A directly invoked future artifact-retention policy skips stored-unindexed
    heads even when the row is otherwise eligible.
11. The request-level same-key/different-digest conflict is owned and
    mechanically required by #646, which observes the Idempotency-Key and
    canonical request digest; ART-004 does not invent a second journal.
12. The configured test-env artifact-blob filesystem plus real-Qdrant
    integration proves bytes-before-head, a real hard-link `EEXIST` adoption,
    exact readback, and deterministic retry ordering. This is VFS-path evidence,
    not a claim that an in-process test proves crash durability.

## Work log

- 2026-08-03 — Claimed #644 after ART-003 merged as #649 / `58309c4`.
  This lane inherits the two boundaries deliberately left by ART-003:
  `publication_version=0`/write-once and the ungated legacy
  `ArtifactPlane.index()` door.
- 2026-08-03 — Aoi approved hard-link no-clobber publication over direct
  `O_EXCL` final writes: a crash can orphan a temp but cannot poison the final
  deterministic address with partial bytes. Request-level same-key/different-
  digest conflict was added to #646's Required failure proof before removal
  here because this content-addressed primitive cannot observe an
  Idempotency-Key. The retention check is explicitly a direct future-policy
  guard, not evidence of a live artifact TTL.
- 2026-08-03 — Test-first baseline is 11 intended failures and 3 passing
  retention controls. Ten escrow cases fail because the new module is absent;
  the retention case reaches current production behavior and observes two
  purges instead of the required one. `tc-coverage` accounts for all 36 source-
  artifact bullets. The initial coroutine-concurrency test was rejected before
  commit because synchronous filesystem work could serialize it; the contract
  now injects a real hard-link winner and proves the `EEXIST` adoption path,
  exact retry, and temp cleanup.
- 2026-08-03 — Invalidated the first test-contract head before review: its
  “write/readback failure” claim injected readback only. The replacement
  callback raises on the first file `fsync`, proves the complete temp bytes were
  present at that exact point, and requires no final address and no artifact
  head. A red count without the write-side failure would have overstated the
  crash contract.
- 2026-08-03 — Aoi's independent attack found the orthogonal axis gap: every
  filesystem proof, including the integration case, used pytest `tmp_path`.
  The replacement integration case reads `ARTIFACT_BLOB_PATH` from the
  authoritative test-env config, uses an isolated child on that same
  filesystem, injects a winner with the real `os.link`, and requires the
  primitive to adopt the resulting real `EEXIST`. The proof is intentionally
  limited to VFS behavior; successful `fsync` cannot prove crash durability
  from inside the process.
- 2026-08-03 — Aoi's fresh `38684fb` review found the shared-root integration
  cleanup was serial: a failed Qdrant purge could mask the original assertion
  and prevent filesystem cleanup. The unique blob child is now removed first
  and unconditionally; Qdrant purge and close are independently protected.
- 2026-08-03 — Production implementation maps the contract narrowly: a
  protocol-based escrow writer publishes temp+file-fsync+hard-link+directory-
  fsync, exact-readback-verifies before head creation, and adopts only an exact
  deterministic head; the legacy index door checks the live head before its
  broad failure handler; retention skips only stored-unindexed artifact rows.
  Focused artifact coverage is 58 passed / 9 named skips. `make check` is 2526
  passed, 195 skipped, 140 deselected, 3 xfailed, all checks passed in 130s.
  The configured-filesystem real-Qdrant integration remains a separate remote
  gate and is not inferred from this local result.
