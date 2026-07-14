---
title: "Slice: ART-001 artifact reindex spike"
slice_id: slice-art-001-artifact-reindex-spikes
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

> tests/docs-only real-Qdrant design-spike for the ART-001 defect. NO source authorization; production code is forbidden in this slice. The spike runs against an ephemeral local Docker Qdrant pinned to qdrant/qdrant:v1.17.1 with verified per-architecture digests (linux/amd64 `sha256:cd3e42737c684ee516ae5533218be93fd5288f41d0a466ed18dbdc22ef52a000`; linux/arm64 `sha256:3fd57e61606ed61c48c91c4131cba6808f01b0879f5478fd011573189855bba1`) bound to 127.0.0.1 on a collision-free port with a temporary volume/network that is removed on exit. The spike runs the 8-row real-Qdrant matrix and the corrected red/control/wrong-candidate matrix. The spike does NOT close ART-001; ART-001 implementation ownership remains undecided.

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

## 8-row real-Qdrant matrix (per Yua 00:24:28 + 00:43:32)

The spike executes the following 8 rows against a persistent Qdrant (NOT `:memory:`). Per Yua 00:43:32, the withdrawn "force network failure on operation 2" and "lease as ordinary upsert" claims are NOT in this list. The rows below record what is observed vs unproven.

1. **Same-collection `batch_update_points` failure semantics.** The only failure mode the spike can deterministically trigger is client-side network/timeout. The server has no partial-apply semantic the spike can control. **Same-request partial-apply atomicity is UNPROVEN, not simulated.** The spike does NOT call `batch_update_points` transactional/atomic.
2. **`FilterSelector` / monotonic-version CAS for matched updates.** The qdrant-client `FilterSelector` exists; the server applies the matched update; we do NOT have a verifiable "matched N" return value. A filtered `SetPayload` is NOT a fence unless the real API returns a trustworthy matched/modified result that the spike demonstrates under contention. Readback-after-race alone is not CAS proof.
3. **Same-collection write ordering across multiple Qdrant client connections.** Two separate clients serializing writes to the same point do not produce a deterministic ordering signal from Qdrant. Real processes are required for the claim; scheduler-luck gather is not evidence.
4. **Cross-collection transactional claim (re-verify the REV2 withdrawal).** Two batches, one per collection, with a forced failure on the second: NO atomicity. The spike does NOT simulate the failure (per Yua 00:43:32).
5. **Search result consistency after a partial publish.** When a single `batch_update_points` succeeds, the FIRST operation IS visible to subsequent scroll. The spike demonstrates this with a single successful batch. A partial-publish failure is not demonstrated in a single request (per row 1).
6. **Concurrent same-artifact indexing with a fence.** Per Yua 00:43:32, ordinary upsert/presence is NOT claim ownership or CAS. A filtered `SetPayload` is NOT a fence unless the real API returns a trustworthy matched/modified result. The spike falsifies the candidate honestly: a lease as ordinary upsert does NOT serialize concurrent writers. The spike records that an EXTERNAL coordinator OR a durable operation record is required.
7. **Visibility of an in-flight generation.** The current source's `query_by_artifact` does NOT filter by `committed_generation`; the spike records this as a known gap in the current source. The proposed read path with a `committed_generation` filter returns only the committed generation. The spike demonstrates the property using the proposed read path.
8. **Failure-after-publish recovery.** Per Yua 00:43:32, the spike cannot trigger a failure mid-batch in a real Qdrant. The current `ArtifactPlane`'s failure handler marks the artifact as `failed` via `set_payload` on the metadata collection; the chunks are physically in the chunks collection and are queryable. The spike records this as a known gap (the current read path does not filter by `committed_generation`).

## 8 corrected strict desired-property red/control tests (per Yua 00:43:32)

The spike demonstrates, not this slice, but the spike acceptance tests for the spike directory must be:

1. **Property 1: second successful index exposes exactly one committed generation.** RED: same-content re-index (per Yua 00:43:32 "same-content reindex is a red on current source, not a healthy control"). CONTROL: single clean index.
2. **Property 2: re-index from more chunks to fewer removes/hides the old tail.** RED: re-index with shorter text.
3. **Property 3: failure after staging/upsert but before publish leaves the prior committed generation visible and the failed generation invisible.** RED: monkeypatch the Qdrant client `set_payload` to raise on the next call AFTER chunks upsert. Per Yua 00:43:32, property proofs execute against the real container and current source.
4. **Property 4: first-ever failed index with the failure AFTER chunks upsert but BEFORE publish exposes zero partial chunks in the published view.** RED: same one-shot metadata-publication failure shape as property 3, but with no prior committed generation. CONTROL: failure BEFORE chunks upsert (the existing `test_failed_chunking_marks_artifact_state_failed_with_reason` test in `tests/planes/test_artifact.py:156` is the healthy control).
5. **Property 5: deterministic same-artifact concurrency produces the specified single-winner/serialized result, never a mixed generation.** RED: use `asyncio.Event` (or similar barrier rendezvous, NOT bare `asyncio.gather` per Yua REV3 #5) to ensure deterministic ordering; the result must be a single-winner. The deterministic-ID wrong candidate must detect mixed content/generation, stale tail, or publisher loss—not merely doubled counts.
6. **Property 6: different-artifact concurrency remains independent.** CONTROL: schedule two concurrent `index()` calls on different artifacts; both must succeed.
7. **Property 7: retry after ambiguous failure is idempotent.** RED: trigger an ambiguous failure (e.g. via a controlled `set_payload` raise) on the first call; retry; the result must converge to a single generation.
8. **Property 8: metadata `chunk_count` and visible committed chunks agree after each completed/failed/retried/concurrent outcome.** RED: trigger re-index, publication failure, retry, OR concurrency such that the metadata and the visible chunks diverge. The test asserts EQUALITY (per Yua 00:43:32 "the test asserts EQUALITY, not divergence"). CONTROL: a single clean index; metadata says N, `query_by_artifact` returns N; the test PASSES.

## 7 wrong-candidate discriminators (per Yua 00:43:32)

The wrong-candidate tests use a test-only/reference model: a "candidate fix" is a small wrapper around the current `index()` method that implements ONE wrong fix. Each wrong candidate must fail its named acceptance assertion; the correct reference satisfies it. The wrong-candidate tests do NOT modify `src/musubi/`. Per Yua 00:43:32:

1. **Deterministic IDs only (Option A without fence).** Fails Property 5 (concurrent same-artifact race; both writers' generations visible because the second overwrites by chunks_index and the fence is missing). The deterministic-ID wrong candidate must detect mixed content/generation, stale tail, or publisher loss—not merely doubled counts.
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

## Notes (per Yua 00:43:32)

- The "force network failure on operation 2 of a 2-op batch" claim is REMOVED. Per Yua: "We already ruled that operation-specific network failure is not a controllable Qdrant semantic. Record same-request partial-apply atomicity as unproven unless you find a real server-supported, independently reproducible fault mechanism; do not simulate it with a client-side fake and do not call `batch_update_points` transactional/atomic."
- The "durable lease in a third Qdrant collection" claim is REMOVED as a fence. Per Yua: "Ordinary upsert/presence is not claim ownership or CAS. A filtered `SetPayload` is not a fence unless the real API returns a trustworthy matched/modified result that the spike demonstrates under contention. The spike must falsify these candidates honestly and may conclude that an external coordinator or durable operation record is required."

## Work log — 2026-07-14 recovery successor

- Replaced the staged module-wide `integration` skip shortcut; the real-Qdrant
  spike remains part of exact-head CI.
- Re-verified both Qdrant v1.17.1 architecture digests through two independent
  Docker manifest inspection paths and made host architecture selection
  explicit/fail-closed.
- Added an exact one-call regression guard for `_ensure_collection` deletion.
- Replaced the placeholder-only test file with executable 8-row matrix,
  8-property, and 7-wrong-candidate families.
- Local arm64 evidence: `18 passed, 7 xfailed`; `--runxfail` reaches exactly
  seven named property assertions (`7 failed, 18 passed`). Property 6 is the
  healthy green control.
- No `src/`, harem-ops ledger, production host, deploy, readiness promotion,
  or merge action.
