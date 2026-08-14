---
title: TEI reranker batching
section: 05-retrieval
type: spec
status: complete
tags: [section/retrieval, type/spec, status/complete, rerank, batching]
updated: 2026-08-12
reviewed: false
implements: ["src/musubi/embedding/tei.py", "src/musubi/retrieve/rerank.py", "tests/test_embedding.py", "tests/retrieve/test_rerank.py"]
---

# TEI reranker batching

Production TEI clients read `max_client_batch_size` from each deployed
endpoint's `/info` contract at construction time. The reranker therefore
submits candidate texts in batches no larger than the live service accepts;
dense and sparse embedding clients use the same contract. Retrieval callers
may use any valid API limit and do not encode service boundaries themselves.

Discovery validates a positive integer. An unavailable or malformed `/info`
response falls back conservatively to 16 inputs for embedding and 32 candidates
for reranking, preserving startup while avoiding the previously unsafe 64-item
embedding assumption. Direct client construction may still provide an explicit
ceiling for tests or deliberately pinned callers.

Production measurement on 2026-08-14 used the live advertised reranker
ceiling of 32 and 80 candidates (three sequential chunks). Ten runs from the
Musubi consumer container measured 0.091 s minimum, 0.094 s median, and
0.146 s maximum. Every run remained below the RET-015 1.5-second outer rerank
budget; contract discovery therefore removes ceiling drift without making
ordinary three-chunk reranks consume the optional-stage deadline.

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
- `test_lower_deployed_tei_batch_ceiling_overrides_static_client_fallback`
- `test_higher_deployed_tei_batch_ceiling_avoids_silent_under_batching`
- `test_unavailable_or_malformed_tei_batch_contract_uses_safe_fallbacks`
- `test_production_tei_clients_are_built_from_runtime_batch_contract`
