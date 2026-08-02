---
title: "Slice: truthful hybrid retrieval channel controls"
slice_id: slice-issue578-truthful-hybrid-channels
issue: 578
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-review, type/slice, retrieval, hybrid, rrf]
updated: 2026-08-02
reviewed: false
depends-on: [slice-retrieval-hybrid]
blocks: []
---

# Slice: truthful hybrid retrieval channel controls

Remove the false weighting contract from the internal hybrid-search seam. Musubi
uses unweighted server-side RRF by design; callers may enable or disable a channel,
but may not supply a magnitude that the fusion cannot honor.

## Specs to implement

- [[13-decisions/0041-truthful-hybrid-channel-controls]]

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
- `src/musubi/retrieve/orchestration.py` (typed-error classification only)
- `tests/retrieve/test_hybrid.py`
- `tests/retrieve/test_orchestration.py` (typed-error classification regression only)
- `tests/retrieve/test_ret004_fusion_diagnostic.py`
- `tests/retrieve/test_ret007_degradation.py`
- `docs/Musubi/05-retrieval/hybrid-search.md`
- `docs/Musubi/13-decisions/0041-truthful-hybrid-channel-controls.md`
- `docs/Musubi/13-decisions/index.md`
- `docs/Musubi/_slices/slice-issue578-truthful-hybrid-channels.md`
- `docs/Musubi/_inbox/locks/slice-issue578-truthful-hybrid-channels.lock`

## Test Contract

- `test_legacy_weight_keywords_are_rejected_instead_of_silently_ignored`
- `test_both_retrieval_channels_disabled_returns_typed_error`
- `test_dense_disabled_omits_dense_prefetch`
- `test_sparse_disabled_omits_sparse_prefetch`
- `test_dense_only_collection_does_not_encode_sparse`
- `test_sparse_only_request_on_dense_only_collection_returns_typed_error`
- `test_hybrid_search_many_forwards_explicit_channel_controls`

## Work log

- 2026-08-02 — `codex-gpt5` mapped every production caller. Fast and deep use the
  default hybrid path; only tests and the RET-004 diagnostic use the numeric
  parameters, and they use zero/nonzero solely as channel toggles. Requested Aoi's
  architecture challenge before the test-first implementation commit.
- 2026-08-02 — Aoi independently found no external consumers across fleet-tools,
  OpenClaw, LiveKit, Vice, Hermes, voice, or Engawa. The plan deliberately errors
  when sparse-only is requested for a dense-only collection, while preserving the
  normal true/true default as an effective dense query. No legacy keyword shim or
  catch-all is added; Python rejects the removed false affordance directly.
- 2026-08-02 — Test-first baseline: 30 passed, one skipped, six failed for the
  intended missing boolean contract. Implementation: full retrieval suite 240
  passed/20 skipped; Test Contract 7/7; whole-repo `make check` passed with 2,460
  passed, 195 skipped, 136 deselected, five pre-existing expected xfails, strict
  mypy/ruff clean, total coverage 90%, and `hybrid.py` coverage 94%. Handed to Aoi
  for mandatory independent review.
