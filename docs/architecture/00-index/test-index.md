---
title: Test Contract Index
section: 00-index
tags: [section/index, status/complete, tdd, testing, type/index]
type: index
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Test Contract Index

This is the central registry of test contracts. Each module spec has a **Test Contract** section listing the required behaviors. This index aggregates them for traceability.

## How to read this

- Each row points to a spec that defines behaviors.
- A slice is complete when every contract line on its owned specs has a corresponding test file with that behavior covered.
- `impl` = implementation file path (in the codebase, not this vault).
- `tests` = test file path.

## Contracts

| Spec | Contract section | Impl | Tests |
|---|---|---|---|
| [[04-data-model/episodic-memory]] | §Test Contract | `musubi/planes/episodic/*.py` | `tests/planes/test_episodic.py` |
| [[04-data-model/curated-knowledge]] | §Test Contract | `musubi/planes/curated/*.py` | `tests/planes/test_curated.py` |
| [[04-data-model/source-artifact]] | §Test Contract | `musubi/planes/artifact/*.py` | `tests/planes/test_artifact.py` |
| [[04-data-model/synthesized-concept]] | §Test Contract | `musubi/planes/synthesis/*.py` | `tests/planes/test_synthesis.py` |
| [[04-data-model/lifecycle]] | §Test Contract | `musubi/lifecycle/states.py` | `tests/lifecycle/test_states.py` |
| [[05-retrieval/scoring-model]] | §Test Contract | `musubi/retrieval/scoring.py` | `tests/retrieval/test_scoring.py` |
| [[05-retrieval/hybrid-search]] | §Test Contract | `musubi/retrieval/hybrid.py` | `tests/retrieval/test_hybrid.py` |
| [[05-retrieval/fast-path]] | §Test Contract | `musubi/retrieval/fast_path.py` | `tests/retrieval/test_fast_path.py` |
| [[05-retrieval/reranker]] | §Test Contract | `musubi/retrieval/reranker.py` | `tests/retrieval/test_reranker.py` |
| [[05-retrieval/orchestration]] | §Test Contract | `musubi/retrieval/orchestrator.py` | `tests/retrieval/test_orchestrator.py` |
| [[06-ingestion/capture]] | §Test Contract | `musubi/ingestion/capture.py` | `tests/ingestion/test_capture.py` |
| [[06-ingestion/maturation]] | §Test Contract | `musubi/lifecycle/maturation.py` | `tests/lifecycle/test_maturation.py` |
| [[06-ingestion/concept-synthesis]] | §Test Contract | `musubi/lifecycle/synthesis.py` | `tests/lifecycle/test_synthesis.py` |
| [[06-ingestion/promotion]] | §Test Contract | `musubi/lifecycle/promotion.py` | `tests/lifecycle/test_promotion.py` |
| [[06-ingestion/vault-sync]] | §Test Contract | `musubi/vault/sync.py` | `tests/vault/test_sync.py` |
| [[06-ingestion/lifecycle-engine]] | §Test Contract | `musubi/lifecycle/engine.py` | `tests/lifecycle/test_engine.py` |
| [[07-interfaces/canonical-api]] | §Test Contract | `musubi/api/*.py` | `tests/api/` |
| [[07-interfaces/sdk]] | §Test Contract | repo `musubi-sdk-py` | `musubi-sdk-py/tests/` |
| [[07-interfaces/mcp-adapter]] | §Test Contract | repo `musubi-mcp` | `musubi-mcp/tests/` |
| [[07-interfaces/livekit-adapter]] | §Test Contract | repo `musubi-livekit` | `musubi-livekit/tests/` |
| [[07-interfaces/openclaw-adapter]] | §Test Contract | repo `musubi-openclaw` | `musubi-openclaw/tests/` |
| [[10-security/auth]] | §Test Contract | `musubi/auth/*.py` | `tests/auth/` |
| [[10-security/redaction]] | §Test Contract | `musubi/redaction/*.py` | `tests/redaction/` |

## Cross-cutting test suites

- **Contract tests** (`tests/contract/`) — golden-master tests that freeze the canonical API surface. Any breaking change here must bump the API major version.
- **Chaos tests** (`tests/chaos/`) — kill Qdrant mid-write, starve GPU memory, race vault writes. Run nightly in CI.
- **Property tests** (`tests/property/`) — Hypothesis-based tests for scoring monotonicity, lifecycle state transitions, and filter correctness.
- **Latency budgets** (`tests/perf/`) — asserts p95 latencies for fast-path retrieval stay under budget on the reference host profile. Runs as CI smoke; gates release.

## Coverage gates

| File zone | Target | Gate |
|---|---|---|
| `musubi/planes/**` | 90% branch | Blocks merge |
| `musubi/retrieval/**` | 90% branch | Blocks merge |
| `musubi/lifecycle/**` | 85% branch | Blocks merge |
| `musubi/api/**` | 80% branch | Blocks merge |
| `musubi/vault/**` | 85% branch | Blocks merge |
| `musubi/auth/**` | 95% branch | Blocks merge |
| `musubi/adapters/**` | 80% branch | Advisory |

Coverage is measured per-file, not aggregate, so a single under-tested file blocks merge.
