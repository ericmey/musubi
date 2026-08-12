---
title: TEI reranker batching
section: 05-retrieval
type: spec
status: implemented
tags: [section/retrieval, type/spec, status/implemented, rerank, batching]
updated: 2026-08-12
reviewed: false
---

# TEI reranker batching

The TEI reranker client submits candidate texts in batches of at most 32,
matching the deployed reranker's client-batch ceiling. Retrieval callers may
use any valid API limit; they do not encode this service boundary themselves.

TEI response indexes are local to each request and may arrive in score order.
The client restores candidate order within every batch before concatenating
scores into the original global candidate order.

A failure in any batch fails the complete rerank and takes the existing
hybrid-order fallback. Musubi never presents a list that mixes cross-encoder
ordering for one batch with hybrid ordering for another as a healthy result.

## Test Contract

- `test_reranker_batches_candidates_without_exceeding_client_batch_size`
- `test_reranker_preserves_global_candidate_order_across_batches`
- `test_reranker_batch_failure_degrades_the_whole_rerank`
