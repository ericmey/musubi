---
title: "0041: Truthful Hybrid Retrieval Channel Controls"
section: 13-decisions
tags: [architecture, retrieval, hybrid, rrf, type/adr, status/accepted]
type: adr
status: accepted
date: 2026-08-02
updated: 2026-08-02
deciders: [Eric, Aoi, Yua]
---

# 0041: Truthful Hybrid Retrieval Channel Controls

## Context

Musubi normatively uses Qdrant's unweighted server-side Reciprocal Rank Fusion for
dense and sparse retrieval. The internal `hybrid_search` and `hybrid_search_many`
functions nevertheless accepted `dense_weight` and `sparse_weight` floats. Their
magnitudes never reached Qdrant: each value was reduced to `weight > 0.0` and used
only to enable or disable a prefetch leg.

No HTTP, SDK, adapter, or external fleet consumer calls these functions or supplies
the weights. Production fast and deep retrieval use the default two-channel path;
only repository tests and the RET-004 diagnostic use zero/nonzero values to isolate
one channel. Keeping a numeric control therefore preserves no weighting capability
and invites future callers to believe values such as `0.7` tune ranking.

## Decision

Replace the numeric parameters with `dense_enabled: bool = True` and
`sparse_enabled: bool = True`. Do not add a compatibility shim, `**kwargs` catcher,
client-side weighted RRF, DBSF, or score normalization. Removed weight keywords are
rejected by Python's normal unexpected-keyword `TypeError`.

Channel validation uses effective collection capability:

- true/true on a dense-plus-sparse collection queries both legs;
- true/true on a dense-only collection queries dense without sparse encoding;
- false/true on a dense-only collection returns typed
  `no_retrieval_channels` before encoding or Qdrant access;
- false/false returns the same typed error.

The sparse-only dense-collection case is intentionally stricter than the historical
implementation. A request for a channel the collection does not have is not silently
treated as a usable query mode.

## Consequences

Default production ranking is byte-for-byte unchanged at the query construction
boundary: dense and sparse prefetches still feed unweighted server-side RRF. Tests
and diagnostics retain explicit dense-only and sparse-only modes using booleans.
Any future weighting proposal must demonstrate a real retrieval need, define score
or rank calibration, and land as a separate ADR rather than reviving misleading
float parameters.

## Test Contract

1. `test_legacy_weight_keywords_are_rejected_instead_of_silently_ignored`
2. `test_both_retrieval_channels_disabled_returns_typed_error`
3. `test_dense_disabled_omits_dense_prefetch`
4. `test_sparse_disabled_omits_sparse_prefetch`
5. `test_dense_only_collection_does_not_encode_sparse`
6. `test_sparse_only_request_on_dense_only_collection_returns_typed_error`
7. `test_hybrid_search_many_forwards_explicit_channel_controls`
