---
title: Executive Summary
section: 00-index
tags: [section/index, status/complete, summary, tldr, type/index]
type: index
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Executive Summary

> Musubi is a three-plane memory server for a small AI agent fleet, backed by a Qdrant hybrid (dense+sparse) index and an Obsidian vault as the curated-knowledge store of record. It runs as a standalone server with a canonical HTTP/gRPC API; MCP, LiveKit, and OpenClaw are independent downstream adapter projects.

## Recommended architecture (one paragraph)

A single Musubi Core Server process owns all business logic and data-plane access. It exposes one canonical API (HTTP + gRPC). All interfaces — MCP server, LiveKit tools, OpenClaw extension — live in **separate repositories** and call Musubi over this API via a shared **Musubi SDK (Python + TypeScript)**. Memory is separated into three planes: **Episodic** (fast, source-first, Qdrant-primary), **Curated Knowledge** (Obsidian vault as store of record; Qdrant as derived, rebuildable index), and **Source Artifact** (object-store blobs with Qdrant chunk-level index for RAG). A background **Lifecycle Engine** runs maturation, concept synthesis, and promotion/demotion on a schedule, with all mutations versioned and traceable (no silent mutation). Retrieval combines dense + sparse vectors (hybrid) with a weighted score over relevance, recency, importance, maturity, reinforcement, provenance, and duplication/contradiction penalties, followed by a local cross-encoder reranker. **All ML inference runs locally on the host's NVIDIA RTX 3080** (CUDA 13): BGE-M3 for dense embeddings, SPLADE++ for sparse, BGE-reranker-v2-m3 for reranking, and a Qwen2.5-7B-Instruct Q4 model for importance scoring and fact extraction — all served behind Text Embeddings Inference (TEI) and Ollama. Gemini Embedding-001 is retained as an optional cloud path for long-context chunks. Fast-path queries bypass the scoring engine entirely and hit a latency-tuned episodic cache. Deployment is Ansible-driven, Docker Compose-based, and targets a single dedicated Ubuntu Server host (Ryzen 5, 32GB RAM, 10GB VRAM) with clear rebuild boundaries for derived assets. See [[08-deployment/host-profile]].

## Top 5 decisions

1. **Three planes are non-negotiable; the bridge layer is the innovation.** Episodic, Curated, and Artifact planes have different truth models, different write paths, and different retention policies. The **Synthesized Concept** memory type is the bridge — repeated/reinforced ideas in the episodic plane become concept objects that are candidates for promotion into curated knowledge. This is the main path knowledge flows *up* in the system. See [[04-data-model/synthesized-concept]] and [[06-ingestion/concept-synthesis]].
2. **Obsidian vault is the curated-knowledge store of record.** Qdrant's curated index is derived and rebuildable from the vault via a file-watcher + ingestion pipeline. Humans edit markdown; Musubi indexes. Promotions write files; demotions move files to a `_archive/` folder. This means the entire curated plane can be rebuilt from a Git-versioned vault in < 30 minutes. See [[06-ingestion/vault-sync]] and [[13-decisions/0003-obsidian-as-sor]].
3. **Canonical API first; every adapter is a thin client.** MCP, LiveKit, OpenClaw, direct REST — all are independent projects that depend only on the Musubi SDK. This keeps Musubi Core free of protocol-specific logic and lets adapter teams (or adapter coding agents) work in parallel without coordinating on core changes. See [[07-interfaces/canonical-api]] and [[03-system-design/abstraction-boundary]].
4. **Hybrid search (dense + sparse) with named vectors, not just cosine similarity.** Qdrant 1.15+ supports native BM25 server-side and named vectors. We run dense (Gemini 1536-d Matryoshka-trimmed for latency) + sparse (BM25) in a single Qdrant query with server-side fusion. Matches the empirical result from Mem0/Zep that pure-dense retrieval loses 15–25% recall on named entities. See [[05-retrieval/hybrid-search]] and [[13-decisions/0005-hybrid-search]].
5. **Lifecycle is explicit and versioned. No silent mutation.** Every memory object has `created_at`, `updated_at`, `version`, `superseded_by`, `supersedes`, `lineage`, and `state` ∈ {provisional, matured, promoted, demoted, archived}. The lifecycle engine never deletes — it marks demoted and moves to cold storage. Every merge produces a `merged_from` lineage entry. This gives us full replay + audit at the cost of ~20% more storage. See [[04-data-model/lifecycle]] and [[13-decisions/0007-no-silent-mutation]].

## Top 5 risks

1. **GPU VRAM contention between co-resident models.** The dedicated host is a Ryzen 5 + RTX 3080 (10GB VRAM). We plan to run BGE-M3 (~2.3GB fp16), SPLADE++ (~700MB), BGE-reranker-v2-m3 (~2.3GB), and a local 7B Q4 LLM for importance/synthesis (~5GB) concurrently — that is tight. Mitigation: [[08-deployment/gpu-inference-topology]] lays out the exact VRAM budget, model quantization levels, lazy-load policy, and fallback to CPU/Ollama spillover. **Residual risk: medium** — the budget works on paper but needs empirical validation with the real workload.
2. **Obsidian vault write contention.** Humans editing in Obsidian + Musubi writing promotions/demotions can race. Mitigation: Musubi only writes files whose frontmatter has `musubi-managed: true`; humans only edit files with `musubi-managed: false`; Musubi watches with debouncing; all vault writes go through a single serialized writer. See [[06-ingestion/vault-sync]]. **Residual risk: medium.**
3. **Qdrant snapshot gaps.** As of April 2026, Qdrant snapshots are per-node per-collection; single-node restore is well-tested but full-stack disaster recovery is operator-intensive. Mitigation: nightly snapshot to local NAS + S3-compatible offsite + weekly restore-into-scratch test + full vault+artifact backup means we can always rebuild Qdrant from source. See [[09-operations/backup-restore]]. **Residual risk: low** (the artifacts + vault are the canonical source; Qdrant is rebuildable).
4. **Coding-agent concurrency corrupting the vault.** A fleet of coding agents editing the same area (e.g., all writing retrieval code) will produce conflicts, duplicated abstractions, and drifting styles. Mitigation: [[00-index/agent-guardrails]] defines slice boundaries, ownership, and the "one agent per module per slice" rule; plus the [[12-roadmap/phased-plan]] has explicit parallelizable chunks with clean seams. **Residual risk: medium** — humans still need to review PRs.
5. **Model drift across embedding versions.** When we swap from BGE-M3 to a newer model later, vectors are not comparable — queries with the new model against old vectors degrade silently. Mitigation: every dense vector is stored under a named vector keyed by `{model_name}_{model_version}` (e.g., `dense_bge_m3_v1`). Re-embedding is a background migration that writes a second named vector alongside; retrieval can choose which to query. See [[13-decisions/0006-pluggable-embeddings]] and [[11-migration/re-embedding]]. **Residual risk: low.**

## Top 5 next steps

1. **Freeze the canonical API v0.1 contract.** Write the OpenAPI + gRPC proto files in [[07-interfaces/canonical-api]]. Every adapter consumes this; nothing else moves until it's signed off. Gate: [[07-interfaces/contract-tests]] passes.
2. **Migrate POC collections to named-vector + hybrid-ready schema.** Existing `musubi_memories` and `musubi_thoughts` become backward-compat aliases over new collections with named dense + sparse vectors. Zero-downtime via Qdrant aliases. See [[11-migration/phase-1-schema]].
3. **Stand up the Obsidian vault + file-watcher ingestion.** This unlocks curated knowledge. Build the `MusubiVault` Python package (vault reader, frontmatter schema validator, Watchdog-based watcher, initial backfill). See [[06-ingestion/vault-sync]].
4. **Write the Lifecycle Engine skeleton.** A dedicated worker process (separate from the API server) that runs maturation, synthesis, promotion, demotion. Starts as an hourly cron; graduates to event-driven. See [[06-ingestion/lifecycle-engine]].
5. **Bootstrap Ansible deployment.** One playbook that stands up Qdrant + Musubi Core + Lifecycle Worker + vault bind-mount on a fresh Debian host. See [[08-deployment/ansible-layout]].

## How to read this vault

- Every doc has YAML frontmatter declaring its section and tags.
- `[[wikilinks]]` are all relative to the vault root.
- ADRs in [[13-decisions/index]] are the load-bearing choices — each has a **Status**, **Context**, **Decision**, **Consequences**, and **Alternatives considered** section.
- Test contracts are embedded inline in each module spec under a **Test Contract** heading. The consolidated TDD index is [[00-index/test-index]].
- Diagrams are ASCII by design so they round-trip through Obsidian without plugins.
