---
title: "Slice: truthful hybrid retrieval channel controls"
slice_id: slice-issue578-truthful-hybrid-channels
issue: 578
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, retrieval, hybrid, rrf]
updated: 2026-08-02
reviewed: false
depends-on: [slice-retrieval-hybrid]
blocks: []
---

# Slice: truthful hybrid retrieval channel controls

Remove the false weighting contract from the internal hybrid-search seam. Musubi
uses unweighted server-side RRF by design; callers may enable or disable a channel,
but may not supply a magnitude that the fusion cannot honor.

## Decision boundary

- Preserve server-side Qdrant RRF and the default dense-plus-sparse behavior.
- Replace `dense_weight` and `sparse_weight` with explicit `dense_enabled` and
  `sparse_enabled` booleans on `hybrid_search` and `hybrid_search_many`.
- Reject both channels disabled with a typed `no_retrieval_channels` error.
- Preserve collection capability gating: a dense-only collection never encodes or
  queries a sparse vector even when sparse is requested.
- Do not add client-side weighted RRF, DBSF, score normalization, or a new public
  API field. A future weighting proposal requires its own evidence and ADR.

## Owned paths

- `src/musubi/retrieve/hybrid.py`
- `tests/retrieve/test_hybrid.py`
- `tests/retrieve/test_ret004_fusion_diagnostic.py`
- `docs/Musubi/05-retrieval/hybrid-search.md`
- `docs/Musubi/_slices/slice-issue578-truthful-hybrid-channels.md`
- `docs/Musubi/_inbox/locks/slice-issue578-truthful-hybrid-channels.lock`

## Test Contract

- `test_legacy_weight_keywords_are_rejected_instead_of_silently_ignored`
- `test_both_retrieval_channels_disabled_returns_typed_error`
- `test_dense_disabled_omits_dense_prefetch`
- `test_sparse_disabled_omits_sparse_prefetch`
- `test_dense_only_collection_does_not_encode_sparse`
- `test_hybrid_search_many_forwards_explicit_channel_controls`

## Work log

- 2026-08-02 — `codex-gpt5` mapped every production caller. Fast and deep use the
  default hybrid path; only tests and the RET-004 diagnostic use the numeric
  parameters, and they use zero/nonzero solely as channel toggles. Requested Aoi's
  architecture challenge before the test-first implementation commit.

