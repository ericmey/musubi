---
title: Test Contract Index
section: 00-index
tags: [section/index, status/complete, tdd, testing, type/index]
type: index
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Test Contract Index

This is the central registry of test contracts. Each module spec has a **Test Contract** section listing the required behaviors. This index aggregates them for traceability.

## How to read this

- Each row points to a spec that defines behaviors.
- A spec's contract is covered when every bullet is a passing test, a skip with a `deferred to #<issue>` reason, or declared out of scope in the PR (the closure rule in [AGENTS.md](../../../AGENTS.md#test-contract-closure-rule)).
- `impl` = implementation path (in the codebase, not this vault).
- `tests` = the main test files. Many areas also have issue-numbered regression files next to them (for example `tests/lifecycle/test_c6_event_loss.py`).

## Contracts

| Spec | Contract section | Impl | Tests |
|---|---|---|---|
| [[04-data-model/episodic-memory]] | §Test Contract | `src/musubi/planes/episodic/` | `tests/planes/test_episodic.py` |
| [[04-data-model/curated-knowledge]] | §Test Contract | `src/musubi/planes/curated/` | `tests/planes/test_curated.py` |
| [[04-data-model/source-artifact]] | §Test Contract | `src/musubi/planes/artifact/` | `tests/planes/test_artifact.py` |
| [[04-data-model/synthesized-concept]] | §Test Contract | `src/musubi/planes/concept/` | `tests/planes/test_concept.py` |
| [[04-data-model/lifecycle]] | §Test Contract | `src/musubi/lifecycle/transitions.py` | `tests/lifecycle/test_lifecycle.py` |
| [[05-retrieval/scoring-model]] | §Test Contract | `src/musubi/retrieve/scoring.py` | `tests/retrieve/test_scoring.py` |
| [[05-retrieval/hybrid-search]] | §Test Contract | `src/musubi/retrieve/hybrid.py` | `tests/retrieve/test_hybrid.py` |
| [[05-retrieval/fast-path]] | §Test Contract | `src/musubi/retrieve/fast.py` | `tests/retrieve/test_fast.py` |
| [[05-retrieval/reranker]] | §Test Contract | `src/musubi/retrieve/rerank.py` | `tests/retrieve/test_rerank.py` |
| [[05-retrieval/orchestration]] | §Test Contract | `src/musubi/retrieve/orchestration.py` | `tests/retrieve/test_orchestration.py` |
| [[06-ingestion/capture]] | §Test Contract | `src/musubi/ingestion/capture.py` | `tests/ingestion/test_capture.py` |
| [[06-ingestion/maturation]] | §Test Contract | `src/musubi/lifecycle/maturation.py` | `tests/lifecycle/test_maturation.py` |
| [[06-ingestion/concept-synthesis]] | §Test Contract | `src/musubi/lifecycle/synthesis.py` | `tests/lifecycle/test_synthesis.py` |
| [[06-ingestion/promotion]] | §Test Contract | `src/musubi/lifecycle/promotion.py` | `tests/lifecycle/test_promotion.py` |
| [[06-ingestion/vault-sync]] | §Test Contract | `src/musubi/vault/reconciler.py`, `src/musubi/vault/watcher.py` | `tests/vault/test_sync.py`, `tests/vault/test_reconciler.py` |
| [[06-ingestion/lifecycle-engine]] | §Test Contract | `src/musubi/lifecycle/runner.py`, `src/musubi/lifecycle/scheduler.py` | `tests/lifecycle/test_runner.py` |
| [[07-interfaces/canonical-api]] | §Test Contract | `src/musubi/api/` | `tests/api/` |
| [[07-interfaces/sdk]] | §Test Contract | `src/musubi/sdk/` (published as `sourceblender/musubi-sdk`) | `tests/sdk/` |
| [[07-interfaces/mcp-adapter]] | §Test Contract | `src/musubi/adapters/mcp/` | `tests/adapters/test_mcp.py`, `tests/adapters/test_mcp_canonical_tools.py` |
| [[07-interfaces/livekit-adapter]] | in that repo | repo `sourceblender/musubi-livekit` | that repo's tests |
| [[07-interfaces/openclaw-adapter]] | in that repo | repo `sourceblender/musubi-openclaw` | that repo's tests |
| [[10-security/auth]] | §Test Contract | `src/musubi/auth/` | `tests/auth/`, `tests/api/test_auth001_token_scope.py` |

## Cross-cutting test suites

- **Integration tests** carry the `integration` marker and run against a Docker test stack (Qdrant, TEI, Ollama) via `make test-integration` and the nightly integration workflow.
- **Retrieval evals** (`tests/evals/`, `src/musubi/evals/`): a deterministic smoke gate on every PR and a scheduled live quality gate (`.github/workflows/evals.yml`).

## Coverage gate

One global floor is enforced: 85% branch coverage (`fail_under = 85` in `pyproject.toml`). Changes under `src/musubi/planes/**` and `src/musubi/retrieve/**` are expected to reach 90%, checked in review. There are no per-module CI gates.
