---
title: "Slice: Embedding client layer"
slice_id: slice-embedding
section: _slices
type: slice
status: in-review
owner: cowork-auto
phase: "2 Hybrid"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-18
reviewed: false
depends-on: ["[[_slices/slice-config]]"]
blocks: ["[[_slices/slice-ingestion-capture]]", "[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-rerank]]"]
---
# Slice: Embedding client layer

> TEI (BGE-M3 dense, SPLADE++ sparse) + optional Gemini fallback. Named vectors: `{model}_{version}`.

**Phase:** 2 Hybrid · **Status:** `in-review` · **Owner:** `cowork-auto`

## Specs to implement

- [[06-ingestion/embedding-strategy]]
- [[08-deployment/gpu-inference-topology]]

## Owned paths (you MAY write here)

  - `musubi/embedding/`
  - `tests/test_embedding.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/retrieve/`
  - `musubi/planes/`

## Depends on

  - [[_slices/slice-config]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-retrieval-hybrid]]
  - [[_slices/slice-ingestion-capture]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-18 — cowork-auto — first cut landed; `status: ready → in-review`

Cowork shipped the first cut during the unsupervised session on 2026-04-18, landing as direct commits to `v2` (pre-branch-protection, pre-full-PR-lifecycle). Commits:

- `bee841f` — `test(embedding): initial test contract for slice-embedding`
- `fd02c91` — `feat(embedding): Embedder protocol, TEI clients, and FakeEmbedder`

Delivery:
- `src/musubi/embedding/base.py` — `Embedder` runtime-checkable Protocol (3 methods: `embed_dense` / `embed_sparse` / `rerank`) + `EmbeddingError` with `status_code` for network-vs-server-error distinction.
- `src/musubi/embedding/tei.py` — `TEIDenseClient` / `TEISparseClient` / `TEIRerankerClient` (httpx-backed, per-service base-URL, 4xx/5xx/network-error handling, reranker response-reorder-by-index).
- `src/musubi/embedding/fake.py` — `FakeEmbedder` (SHA-256-seeded deterministic vectors, L2-normalised dense output for cosine-ready tests).
- `tests/test_embedding.py` — 22 tests (protocol + Fake determinism + unit-norm + TEI call-shape + 5xx/4xx/timeout/connect-error paths + empty-input + custom-timeout).

Test Contract Closure state via `make tc-coverage SLICE=slice-embedding`:

- **Partially out-of-scope** — referenced specs (`embedding-strategy.md`, `gpu-inference-topology.md`) contain Test Contract bullets that belong to **other slices** (retrieval-hybrid for fusion + caching, ops-compose / ops-observability for GPU topology + VRAM budgets + service-degradation integration tests). The tool reports 24 "missing" bullets; most are legitimately out-of-scope for the embedding-protocol slice.
- **Genuine in-scope deferrals** (tracked in a follow-up Issue — see Cross-slice tickets below):
  - `test_batch_encode_64_items_one_call` — batching logic not yet implemented.
  - `test_truncate_content_to_2048_chars` — input truncation not yet enforced.
  - `test_query_cache_hit_on_repeat` + `test_query_cache_miss_on_different_query` + `test_query_cache_cleared_on_model_revision_change` — request cache not yet built.
  - Retry-with-backoff on transient 5xx (from `embedding-strategy.md`) — currently raises on first failure.

The **review pass** + the follow-up work to close the genuine in-scope deferrals are both prerequisites for flipping to `done`.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
