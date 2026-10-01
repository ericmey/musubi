---
title: Executive Summary
section: 00-index
tags: [section/index, status/complete, summary, tldr, type/index]
type: index
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# Executive Summary

> Musubi is a three-plane memory server for a small team of AI agents (small team / single-operator: 1–5 humans, one host). It is backed by a Qdrant hybrid (dense + sparse) index and an Obsidian vault as the curated-knowledge store of record. It runs as a standalone server with one canonical HTTP API; MCP, the CLI and the Python SDK ship in this repo, and the other integrations are separate repositories that call that API.

## Recommended architecture (one paragraph)

A single Musubi Core process owns all business logic and data-plane access. It exposes one canonical HTTP API (`openapi.yaml` at the repo root). The Python SDK (`src/musubi/sdk`, also published as `sourceblender/musubi-sdk`), the MCP server (`src/musubi/adapters/mcp`) and the `musubi` CLI are in this repo; LiveKit, OpenClaw and the other agent integrations live in separate repositories and call the same API. Memory is separated into three planes: **Episodic** (fast, source-first, Qdrant-primary), **Curated Knowledge** (Obsidian vault as store of record; Qdrant as derived, rebuildable index) and **Source Artifact** (blobs on disk under `ARTIFACT_BLOB_PATH` with a chunk-level Qdrant index). A separate **lifecycle worker** runs maturation, concept synthesis, promotion, demotion, reflection and vault reconciliation on a schedule, with every state change recorded as a lifecycle event (no silent mutation). Retrieval combines dense and sparse vectors in one Qdrant query fused server-side with RRF, applies a weighted score over relevance, recency, importance, provenance (plane and lifecycle state) and reinforcement (`src/musubi/retrieve/scoring.py`), and can rerank with a cross-encoder. Inference is self-hosted: BGE-M3 (1024-d) for dense embeddings, SPLADE v3 (`naver/splade-v3`) for sparse, `BAAI/bge-reranker-v2-m3` for reranking, all served by Text Embeddings Inference (TEI), and an Ollama-served LLM (`LLM_MODEL`, `qwen3:4b` in `.env.example`) for importance scoring, synthesis and promotion; the lifecycle LLM can instead point at any OpenAI-compatible endpoint ([[13-decisions/0043-lifecycle-llm-openai-compatible-endpoint|ADR 0043]]). The inference services run either on the same host through the optional GPU Compose overlay or on remote endpoints the operator supplies. Fast-path queries use a reduced pipeline with a short-lived response cache. Deployment is Docker Compose on an operator-prepared Linux host, with clear rebuild boundaries for derived assets. See [[08-deployment/compose-stack]] and [[08-deployment/host-profile]].

## Top 5 decisions

1. **Three planes are non-negotiable; the bridge layer is the innovation.** Episodic, Curated and Artifact planes have different truth models, different write paths and different retention policies. The **Synthesized Concept** memory type is the bridge: repeated or reinforced ideas in the episodic plane become concept objects that are candidates for promotion into curated knowledge. This is the main path knowledge flows *up* in the system. See [[04-data-model/synthesized-concept]] and [[06-ingestion/concept-synthesis]].
2. **Obsidian vault is the curated-knowledge store of record.** Qdrant's curated index is derived and rebuildable from the vault. Humans edit markdown; Musubi indexes it. Promotion writes vault files through a single writer (`src/musubi/vault/writer.py`), and the lifecycle worker's `vault_reconcile` job re-syncs the vault into Qdrant every 6 hours. See [[06-ingestion/vault-sync]] and [[13-decisions/0003-obsidian-as-sor]].
3. **Canonical API first; every integration is a thin client.** MCP, the CLI, LiveKit, OpenClaw and direct HTTP all go through the same API, so Musubi Core stays free of protocol-specific logic. See [[07-interfaces/canonical-api]] and [[03-system-design/abstraction-boundary]].
4. **Hybrid search (dense + sparse) with named vectors, not just cosine similarity.** Every collection stores a named dense vector (`dense_bge_m3_v1`, BGE-M3, 1024-d) and a named sparse vector (`sparse_splade_v1`, SPLADE v3), queried together with Qdrant server-side RRF fusion (`src/musubi/store/specs.py`, `src/musubi/retrieve/hybrid.py`). Sparse retrieval protects recall on names and exact terms, where pure-dense retrieval is weakest. See [[05-retrieval/hybrid-search]] and [[13-decisions/0005-hybrid-search]].
5. **Lifecycle is explicit and versioned. No silent mutation.** Every memory object carries `created_at`, `updated_at`, `version`, lineage fields and a `state` (`provisional`, `matured`, `promoted`, `synthesized`, `demoted`, `archived`, `superseded`; `src/musubi/types/common.py`). Demotion and archival change state; they do not delete. Every transition is written as a lifecycle event. See [[04-data-model/lifecycle]] and [[13-decisions/0007-no-silent-mutation]].

## Top 5 risks

1. **GPU VRAM contention between co-resident models.** When all inference runs locally, three TEI models and the LLM share one GPU. On the measured reference host (Ryzen 5, 32 GB RAM, RTX 3080 10 GB) that is tight. Mitigation: a small LLM by default, and the option to run any inference service on a remote endpoint instead. See [[08-deployment/gpu-inference-topology]]. **Residual risk: medium.**
2. **Obsidian vault write contention.** Humans editing in Obsidian and Musubi writing promotions can race. Mitigation: promotion never overwrites a file that lacks `musubi-managed: true`; it writes a sibling file instead (`src/musubi/lifecycle/promotion.py`). Musubi's vault writes go through one atomic writer that records a write log, and reconciliation is periodic. See [[06-ingestion/vault-sync]]. **Residual risk: medium.**
3. **Qdrant snapshot gaps.** Qdrant snapshots are per collection, and full-stack restore is operator work. Mitigation: the vault and artifact blobs are the canonical sources, so Qdrant can be rebuilt from them; the backup helper defines cadences (vault 15 min, artifact blobs and SQLite 60 min, Qdrant 6 h) and a 90-day restore drill (`src/musubi/ops/backup.py`). See [[09-operations/backup-restore]]. **Residual risk: low.**
4. **Concurrent contributors drifting the code.** Several coding agents editing the same area produce conflicts, duplicated abstractions and drifting styles. Mitigation: work runs through issues and small PRs, each reviewed by someone other than its author, under the contract in AGENTS.md. **Residual risk: medium**; review is still the control.
5. **Model drift across embedding versions.** Vectors from a new embedding model are not comparable with old ones. Mitigation: vectors are stored under model-keyed names (`dense_bge_m3_v1`), so a re-embedding migration can write a second named vector alongside the first. See [[13-decisions/0006-pluggable-embeddings]] and [[11-migration/re-embedding]]. **Residual risk: low.**

## What's next

Open work is tracked as GitHub issues. Longer-range direction is in [[12-roadmap/index|Roadmap]].

## How to read this vault

- Every doc has YAML frontmatter declaring its section and tags.
- `[[wikilinks]]` are all relative to the vault root.
- ADRs in [[13-decisions/index]] are the load-bearing choices. Each has a **Status**, **Context**, **Decision**, **Consequences** and **Alternatives considered** section.
- Test contracts are embedded inline in each module spec under a **Test Contract** heading. The consolidated index is [[00-index/test-index]].
- Diagrams are ASCII by design so they round-trip through Obsidian without plugins.
