---
title: "ADR 0012: Local Inference on Dedicated GPU, Not Hosted APIs"
section: 13-decisions
tags: [adr, inference, local-first, privacy, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0012: Local Inference on Dedicated GPU, Not Hosted APIs

**Status:** accepted
**Date:** 2026-03-20
**Deciders:** Eric

## Context

Musubi needs three flavors of inference:

1. **Embeddings** — dense (BGE-M3) + sparse (SPLADE++ V3). Called on every capture and every retrieval.
2. **Reranker** — cross-encoder (BGE-reranker-v2-m3). Called on deep retrievals.
3. **LLM** — synthesis, maturation, promotion drafts, reflections. Called in background lifecycle jobs, sometimes interactively.

All three can be hosted (Gemini, OpenAI, Anthropic) or local (TEI for embeddings+rerank, Ollama/vLLM for LLM).

Considerations:

- **Privacy.** Musubi ingests household-scale personal content: conversations with agents, personal notes, medical, financial. Sending every piece of that to a hosted API is a material privacy posture shift. We're not fundamentally against hosted inference, but it should not be the default for household content.
- **Latency.** Hosted APIs have network round-trip + queueing. Local inference on a dedicated GPU is faster for small batches.
- **Cost.** Embeddings at ~5k memories/day × 2 models is free locally. Hosted: small but not zero ($5-20/month depending on provider).
- **Determinism.** Local models are pinned. Hosted models change under you.
- **Availability.** Local doesn't require network. Household still works if the ISP blips.
- **Capability.** For a 7B-class LLM, top hosted models (Claude, GPT-4) are meaningfully smarter than Qwen2.5-7B. But for maturation / synthesis of *our own corpus*, 7B is good enough; we're not asking it to invent.

The dedicated host has an RTX 3080 (10GB VRAM). VRAM budget ([[08-deployment/gpu-inference-topology]]):

- BGE-M3 (INT8): ~1.0GB.
- SPLADE++ V3 (INT8): ~0.8GB.
- BGE-reranker-v2-m3 (INT8): ~1.2GB.
- Qwen2.5-7B Q4_K_M: ~5.5GB.
- Overhead + headroom: ~1.1GB.

Tight but workable at `OLLAMA_NUM_PARALLEL=1`.

## Decision

**All v1 inference runs locally on the dedicated GPU.** Hosted APIs are not called by default for any Musubi workflow.

Details:

- **TEI** for dense+sparse embeddings and reranker. Two containers (embed + rerank).
- **Ollama** for LLM (Qwen2.5-7B-Instruct Q4_K_M).
- Embedding provider abstraction ([[13-decisions/0006-pluggable-embeddings]]) permits a hosted-API provider per named vector if operator opts in for a specific collection.
- **Opt-in hosted mode** exists for LLM only: `OLLAMA_FALLBACK=anthropic` env flag routes to Claude when Ollama is unavailable. Off by default.

No content leaves the host to a third-party inference API in the default configuration.

## Alternatives

**A. Hybrid: embeddings local, LLM hosted.** Saves VRAM for a fourth model or a bigger Qwen quant. Trades privacy for capability. Not chosen for v1; reconsider for v2 if LLM quality is the bottleneck.

**B. All hosted.** Cheapest compute capital. Worst privacy.

**C. Local embeddings, no LLM-backed lifecycle jobs.** Skips synthesis/maturation/reflection rewrites. Makes curated promotion fully manual. Not crazy, but loses a lot of the "it writes things for you" value.

**D. Run everything on CPU (no GPU).** BGE-M3 and SPLADE are tolerable on CPU (~10x slower). Reranker is unpleasant (~50x slower). LLM on CPU is ~100x slower — unusable for lifecycle batches. Rejected.

## Consequences

- Privacy story is clean: everything stays on the host by default.
- Ops burden includes keeping TEI + Ollama healthy, monitoring GPU, and managing the CUDA runtime.
- LLM capability is capped at what fits in VRAM. Upgrade path: bigger GPU → more VRAM → bigger models ([[11-migration/scaling]]).
- For non-routine work (e.g., drafting a long report for a person, not for the memory pipeline), the user can point adapters at a hosted API directly; that's out of scope for Musubi proper.

Trade-offs:

- Qwen2.5-7B writes less well than Claude or GPT-4. For maturation/synthesis, this is tolerable; for promotion drafts, operator review catches the rough edges.
- A GPU failure takes the whole inference stack offline. Mitigated by Ansible rebuild playbook + pinned model versions ([[09-operations/runbooks]]).

## Links

- [[06-ingestion/embedding-strategy]]
- [[08-deployment/gpu-inference-topology]]
- [[13-decisions/0006-pluggable-embeddings]]
- [[10-security/data-handling]]
