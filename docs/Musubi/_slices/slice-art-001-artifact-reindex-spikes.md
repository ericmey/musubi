---
title: "Slice: ART-001 artifact reindex spike"
slice_id: slice-art-001-artifact-generation-spikes
issue: 451
section: _slices
type: slice
status: in-progress
owner: tama
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice, spike]
updated: 2026-07-14
reviewed: false
depends-on: []
blocks: []
---

# Slice: ART-001 artifact reindex spike

> tests/docs-only real-Qdrant design-spike for the ART-001 defect. NO source authorization; production code is forbidden in this slice. The spike runs against an ephemeral local Docker Qdrant pinned to qdrant/qdrant:v1.17.1 (linux/arm64, sha256:3fd57e61606ed61c48c91c4131cba6808f01b0879f5478fd011573189855bba1) bound to 127.0.0.1 on a collision-free port with a temporary volume/network that is removed on exit. The spike runs the 8-row real-Qdrant matrix from the REV3 audit packet and the corrected red/control/wrong-candidate matrix to determine the option choice for the ART-001 fix. The spike does NOT close ART-001; ART-001 implementation ownership remains undecided after the spike.

**Phase:** 4 Planes · **Status:** `in-progress` · **Owner:** `tama`

## Specs to implement

- The ART-001 issue (#451) acceptance invariants; the spike demonstrates them (or refutes them) against a real persistent Qdrant deployment.

## Owned paths

- `tests/planes/artifact/test_artifact_reindex_spikes.py` (the spike acceptance file; the existing `tests/planes/test_artifact.py` is NOT modified unless strictly necessary, per Yua 00:24:28)
- `spike-notes/` (this spike's own notes directory; sequencing deviation, digest records, observed results, what remains unknowable, recommended architecture)
- `docs/Musubi/_slices/slice-art-001-artifact-reindex-spikes.md` (this file)
- `docs/Musubi/_inbox/locks/slice-art-001-artifact-reindex-spikes.lock` (the lock)

## Forbidden paths

- `src/musubi/planes/artifact/` (any source modifications; per Yua 00:24:28 "Explicitly forbid `src/**` for this phase")
- `src/**` (production source is forbidden in the spike phase)
- `tests/planes/test_artifact.py` unless strictly necessary (per Yua 00:24:28 "the existing `tests/planes/test_artifact.py` only if necessary")
- any harem-ops file (no harem-ops mirror; the central ledger at `harem-ops/projects/active/hermes-musubi-provider/audits/2026-07-12-musubi-system-defect-ledger.md` owns cross-project status)

## Spike matrix (the 8 rows from the REV3 audit)

The spike must execute the following 8 rows against a persistent Qdrant (NOT `:memory:`):

1. **Same-collection atomic-batch (`batch_update_points`) failure semantics.** Force failure on operation 2 of a 2-op batch via network fault injection. Does Qdrant roll back operation 1?
2. **`FilterSelector` / monotonic-version CAS for matched updates.** A filtered `SetPayload` with `version={N}` and forced mismatch. Does Qdrant return a verifiable failure, or silently apply?
3. **Same-collection write ordering across multiple Qdrant client connections.** Two writers, two different processes. Determinism?
4. **Cross-collection transactional claim** (re-verify the REV2 withdrawal). Two batches, one per collection, with forced failure on the second. NO atomicity.
5. **Search result consistency after a partial publish.** After a failed `batch_update_points`, does the first op become visible to a subsequent `scroll`?
6. **Concurrent same-artifact indexing with a fence.** A per-artifact durable lease in a third Qdrant collection. Do two writers race correctly?
7. **Visibility of an in-flight generation.** During the publish window, does the read path see the prior generation only?
8. **Failure-after-publish recovery.** Retry after a failed publish: is the prior generation queryable, the failed generation uncommitted?

## Corrected strict desired-property red/control matrix (the 8 invariants)

The spike must demonstrate, not this slice, but the spike acceptance tests for the spike directory must be:

1. **Property 1: second successful index exposes exactly one committed generation.** RED: same-content re-index. CONTROL: single clean index.
2. **Property 2: re-index from more chunks to fewer removes/hides the old tail.** RED: re-index with shorter text.
3. **Property 3: failure after staging/upsert but before publish leaves the prior committed generation visible and the failed generation invisible.** RED: monkeypatch the Qdrant client `set_payload` to raise on the next call AFTER chunks upsert.
4. **Property 4: first-ever failed index with the failure AFTER chunks upsert but BEFORE publish exposes zero partial chunks in the published view.** RED: same one-shot metadata-publication failure shape as property 3, but with no prior committed generation. CONTROL: failure BEFORE chunks upsert (the existing `test_failed_chunking_marks_artifact_state_failed_with_reason` test in `tests/planes/test_artifact.py:156` is the healthy control).
5. **Property 5: deterministic same-artifact concurrency produces the specified single-winner/serialized result, never a mixed generation.** RED: use `asyncio.Event` (or similar barrier rendezvous, NOT bare `asyncio.gather` per Yua REV3 #5) to ensure deterministic ordering; the result must be a single-winner.
6. **Property 6: different-artifact concurrency remains independent.** CONTROL: schedule two concurrent `index()` calls on different artifacts; both must succeed.
7. **Property 7: retry after ambiguous failure is idempotent.** RED: trigger an ambiguous failure (e.g. via a controlled `set_payload` raise) on the first call; retry; the result must converge to a single generation.
8. **Property 8: metadata `chunk_count` and visible committed chunks agree after each completed/failed/retried/concurrent outcome.** RED: trigger re-index, publication failure, retry, OR concurrency such that the metadata and the visible chunks diverge. The test asserts EQUALITY, not divergence (per Yua REV3 #4). CONTROL: a single clean index; metadata says N, `query_by_artifact` returns N; the test PASSES.

## Per-wrong intended-assertion matrix (the 7 wrong candidates from the REV3 audit)

The wrong-candidate tests use a test-only/reference model: a "candidate fix" is a small wrapper around the current `index()` method that implements ONE wrong fix. Each wrong candidate fails its named acceptance assertion; the correct reference satisfies it. The wrong-candidate tests do NOT modify `src/musubi/`.

1. **Deterministic IDs only (Option A without fence).** Fails Property 5 (concurrent same-artifact race; both writers' generations visible because the second overwrites by chunks_index and the fence is missing).
2. **Delete-before-upsert (compensating cleanup without fence).** Fails Property 5 (concurrent race; both writers' delete-then-upsert interleave; loser wipes the winner).
3. **Upsert-before-delete (competing order; dangerous).** Fails Property 3 (publish failure; prior generation left visible because the loser's delete ran first).
4. **Generation pointer without read filtering.** Fails Property 1 (second index without fence re-points the metadata; reads see the new generation but the prior generation's chunks are still in the collection; the read returns both).
5. **Unfenced last-writer-wins generation switch.** Fails Property 5 (concurrent; both writers' generations are visible; metadata races).
6. **Compensating rollback that deletes a concurrent winner.** Fails Property 5 (a loser that cleans up at the end of `index()` may wipe the winner's chunks; the loser's lease must prevent this).
7. **Bare `asyncio.gather` without rendezvous.** NOT a production wrong fix; it is a TEST HARNESS weakness. Property 5's red-test must reject this harness via a structural/determinism guard or by replacing with `asyncio.Event` rendezvous; if the red-test cannot reject bare `asyncio.gather`, the test design is wrong.

## Status

Do NOT mark this slice ready, merge it, implement source, touch host/deploy, or change the central ledger until Yua accepts the spike output. The spike recommends an architecture; ART-001 implementation ownership remains undecided.

## Ownership

Owner: tama (the spike is a tests/docs-only deliverable). ART-001 implementation ownership remains unassigned.
