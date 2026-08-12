---
title: "Slice: batch reranker requests within the TEI client ceiling"
slice_id: slice-ret-017-reranker-batching
issue: 687
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "3 Retrieval"
tags: [section/slices, status/in-progress, type/slice, retrieval, rerank]
updated: 2026-08-12
reviewed: false
depends-on: [slice-ret015-blended-deadline]
blocks: []
---

# Slice: batch reranker requests within the TEI client ceiling

Repair the production `reranker_failed` degradation that begins at ordinary
retrieval limits because deep retrieval over-fetches four candidates per
requested result while TEI accepts at most 32 texts per rerank request.

## Production evidence

- `limit=8` sends 32 candidates and reranks successfully.
- `limit=10` sends 40 candidates and TEI returns HTTP 413:
  `batch size 40 > maximum allowed batch size 32`.
- `limit=20` sends 80 candidates and returns the same HTTP 413.
- The failure reproduces in about 0.53 seconds, well below the 1.5-second
  rerank budget; this is request validation, not the RET-015 timeout.
- `/v1/ops/status` reports the reranker healthy because its health probe proves
  liveness, not that an arbitrary batch is admissible.

## Decision boundary

- Preserve the canonical retrieve API and all valid request limits.
- Batch inside `TEIRerankerClient`, the boundary that knows the TEI request
  shape, rather than encoding the service ceiling in every caller.
- Preserve score order across batches even when TEI returns each batch in
  score order.
- Any failed batch degrades the whole rerank exactly once; partial reranking is
  not surfaced as healthy ranking.
- Do not lower the Hermes client cap to encode a server implementation detail.

## Owned paths

- `src/musubi/embedding/tei.py` (reranker batching only)
- `src/musubi/retrieve/rerank.py` (accurate degradation log only)
- `tests/test_embedding.py` (reranker batching contract only)
- `tests/retrieve/test_rerank.py` (degradation wording only)
- `docs/Musubi/05-retrieval/deep-path.md` (reranker batching contract only)
- `docs/Musubi/_slices/slice-ret-017-reranker-batching.md`
- `docs/Musubi/_inbox/locks/slice-ret-017-reranker-batching.lock`

## Forbidden paths

- `src/musubi/api/`
- `src/musubi/planes/`
- `src/musubi/types/`
- Deployment manifests and live-host configuration
- Hermes and OpenClaw client plugins

## Test Contract

- `test_reranker_batches_candidates_without_exceeding_client_batch_size`
- `test_reranker_preserves_global_candidate_order_across_batches`
- `test_reranker_batch_failure_degrades_the_whole_rerank`

## Work log

- 2026-08-12 — `codex-gpt5` reproduced the exact production boundary from
  Musubi logs, claimed Issue #687, and opened draft PR #688 before code changes.

