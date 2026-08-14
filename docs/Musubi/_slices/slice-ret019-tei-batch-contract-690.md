---
title: "Slice: derive TEI batch ceilings from the live service contract"
slice_id: slice-ret019-tei-batch-contract-690
issue: 690
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "3 Retrieval"
tags: [section/slices, status/in-progress, type/slice, retrieval, embedding, rerank]
updated: 2026-08-14
reviewed: false
depends-on: [slice-ret-017-reranker-batching]
blocks: []
---

# Slice: derive TEI batch ceilings from the live service contract

The client-side dense, sparse, and reranker batch ceilings are static values,
while each deployed TEI service independently exposes its actual
`max_client_batch_size` through `/info`. Divergence either produces HTTP 413
degradation or silently adds avoidable sequential requests.

## Decision boundary

- Read each endpoint's `max_client_batch_size` from `/info` when production
  clients are constructed.
- Validate the value as a positive integer before using it.
- Fall back to conservative static ceilings when discovery is unavailable or
  malformed; discovery failure must not prevent Musubi from starting.
- Preserve direct client construction with an explicit batch ceiling for unit
  tests and deliberately pinned callers.
- Do not change the canonical retrieval API or the whole-rerank fallback.

## Owned paths

- `src/musubi/embedding/tei.py`
- `src/musubi/embedding/__init__.py`
- `src/musubi/api/bootstrap.py` (TEI construction only)
- `src/musubi/lifecycle/runner.py` (TEI construction only)
- `src/musubi/vault/runtime.py` (TEI construction only)
- `src/musubi/evals/live_gate.py` (TEI construction only)
- `tests/test_embedding.py`
- `docs/Musubi/05-retrieval/reranker-batching.md`
- `docs/Musubi/_slices/slice-ret019-tei-batch-contract-690.md`

## Forbidden paths

- Canonical API schemas and routers
- Retrieval ranking and warning semantics
- Deployment manifests and live-host configuration
- Plane and store implementations

## Test Contract

- `test_lower_deployed_tei_batch_ceiling_overrides_static_client_fallback`
- `test_higher_deployed_tei_batch_ceiling_avoids_silent_under_batching`
- `test_unavailable_or_malformed_tei_batch_contract_uses_safe_fallbacks`
- `test_production_tei_clients_are_built_from_runtime_batch_contract`

## Work log

- 2026-08-14 — `codex-gpt5` verified the production TEI 1.2.0 `/info`
  contract from the Musubi consumer host: dense 16, sparse 32, and reranker 32.
  Tests were written before implementation for lower, higher, malformed, and
  unreachable contracts plus all four production construction surfaces.
- 2026-08-14 — The production factory now validates each `/info` ceiling and
  uses conservative 16/16/32 fallbacks without changing direct-client pinned
  behavior. A live 80-candidate rerank at the advertised 32-item ceiling ran
  as three sequential chunks: ten runs measured 0.091 s minimum, 0.094 s
  median, and 0.146 s maximum, all below the 1.5-second RET-015 budget.
