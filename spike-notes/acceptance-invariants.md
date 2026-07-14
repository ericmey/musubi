# ART-001 spike — acceptance invariants stub

This file is a STUB for the 8-row matrix + the 8 corrected
strict desired-property tests + the 7 wrong-candidate tests.
The actual spike work (8-row matrix, especially row 6 concurrent
same-artifact indexing with fence) requires a real persistent
Qdrant deployment (NOT `:memory:`) and the spike acceptance
file at `tests/planes/artifact/test_artifact_reindex_spikes.py`.

## 8-row real-Qdrant matrix (from REV3 audit)

1. **Same-collection atomic-batch (`batch_update_points`) failure
   semantics.** Force failure on operation 2 of a 2-op batch via
   network fault injection. Does Qdrant roll back operation 1?
2. **`FilterSelector` / monotonic-version CAS for matched
   updates.** A filtered `SetPayload` with `version={N}` and
   forced mismatch. Does Qdrant return a verifiable failure, or
   silently apply? (Per Yua REV3 #3: readback-after-race alone is
   not CAS proof.)
3. **Same-collection write ordering across multiple Qdrant
   client connections.** Two writers, two different processes.
   Determinism?
4. **Cross-collection transactional claim** (re-verify the REV2
   withdrawal). Two batches, one per collection, with forced
   failure on the second. NO atomicity. (Per Yua REV2 #1.)
5. **Search result consistency after a partial publish.** After a
   failed `batch_update_points`, does the first op become visible
   to a subsequent `scroll`?
6. **Concurrent same-artifact indexing with a fence.** A
   per-artifact durable lease in a third Qdrant collection. Do
   two writers race correctly? (Per Yua REV3 #1: this is the
   central design question for ART-001.)
7. **Visibility of an in-flight generation.** During the publish
   window, does the read path see the prior generation only?
8. **Failure-after-publish recovery.** Retry after a failed
   publish: is the prior generation queryable, the failed
   generation uncommitted? (Per Yua REV3 #3: this is the trap.)

## Corrected strict desired-property red/control matrix (8)

1. **Property 1: second successful index exposes exactly one
   committed generation.** RED: same-content re-index. CONTROL:
   single clean index. (Per Yua REV3 #4: same-content re-index
   is a red on current source, not a healthy control.)
2. **Property 2: re-index from more chunks to fewer removes/hides
   the old tail.** RED: re-index with shorter text.
3. **Property 3: failure after staging/upsert but before publish
   leaves the prior committed generation visible and the failed
   generation invisible.** RED: monkeypatch the Qdrant client
   `set_payload` to raise on the next call AFTER chunks upsert.
4. **Property 4: first-ever failed index with the failure AFTER
   chunks upsert but BEFORE publish exposes zero partial chunks
   in the published view.** RED: same one-shot metadata-publication
   failure shape as property 3, but with no prior committed
   generation. CONTROL: failure BEFORE chunks upsert.
5. **Property 5: deterministic same-artifact concurrency
   produces the specified single-winner/serialized result, never
   a mixed generation.** RED: use `asyncio.Event` (or similar
   barrier rendezvous, NOT bare `asyncio.gather` per Yua REV3 #5)
   to ensure deterministic ordering.
6. **Property 6: different-artifact concurrency remains
   independent.** CONTROL.
7. **Property 7: retry after ambiguous failure is idempotent.**
   RED: trigger an ambiguous failure (controlled `set_payload`
   raise); retry.
8. **Property 8: metadata `chunk_count` and visible committed
   chunks agree after each completed/failed/retried/concurrent
   outcome.** RED: trigger re-index, publication failure, retry,
   OR concurrency such that the metadata and the visible chunks
   diverge. The test asserts EQUALITY (per Yua REV3 #4), not
   divergence. CONTROL: a single clean index.

## Per-wrong intended-assertion matrix (7 wrong candidates)

1. **Deterministic IDs only (Option A without fence).** Fails
   Property 5 (concurrent race; both writers' generations
   visible because the second overwrites by chunks_index and
   the fence is missing). Discriminator must detect mixed
   content/generation, stale tails, or publisher loss — not
   assume a doubled count (per Yua REV3 #6).
2. **Delete-before-upsert (compensating cleanup without fence).**
   Fails Property 5.
3. **Upsert-before-delete (competing order).** Fails Property 3.
4. **Generation pointer without read filtering.** Fails
   Property 1.
5. **Unfenced last-writer-wins generation switch.** Fails
   Property 5.
6. **Compensating rollback that deletes a concurrent winner.**
   Fails Property 5.
7. **Bare `asyncio.gather` without rendezvous.** NOT a production
   wrong fix; it is a TEST HARNESS weakness. Property 5's
   red-test must reject this harness via a structural/
   determinism guard.

## Spike plan (the actual work)

Each test in `tests/planes/artifact/test_artifact_reindex_spikes.py`
uses the `qdrant_server` module-scoped fixture (real persistent
Qdrant pinned to v1.17.1 on linux/arm64, bound to 127.0.0.1 on
a collision-free port). The container is removed on exit.

The 8-row matrix produces a per-row observation; the 8 properties
produce a per-property red/control outcome; the 7 wrong
candidates produce a per-wrong-candidate failure mode. All of
this is recorded here as the spike completes.

## Status

Do NOT mark this slice ready, merge it, implement source, touch
host/deploy, or change the central ledger until Yua accepts the
spike output. The spike recommends an architecture; ART-001
implementation ownership remains unassigned.
