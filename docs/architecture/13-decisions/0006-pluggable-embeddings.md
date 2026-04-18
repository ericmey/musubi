---
title: "ADR 0006: Named Vectors + Embedding Provider Abstraction"
section: 13-decisions
tags: [adr, embeddings, schema, section/decisions, status/accepted, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0006: Named Vectors + Embedding Provider Abstraction

**Status:** accepted
**Date:** 2026-03-16
**Deciders:** Eric

## Context

The POC used unnamed Qdrant vectors and a single embedding source (Gemini). That made sense at the time: one model, one vector per point, simple.

For v1 we need:

1. Dense + sparse side-by-side (ADR 0005).
2. The ability to add a new embedding model without throwing out the collection.
3. An abstraction that lets us swap providers (TEI, Ollama embedding, cloud API) per model without rewriting capture/retrieval paths.

Qdrant 1.8+ supports **named vectors**: a single point has multiple named vector fields (e.g., `dense_bge_m3_v1`, `sparse_splade_v1`, later `dense_bge_m4_v1`). Each can be queried independently, fused in a single query via `Prefetch`+`FusionQuery`, and added/removed per-collection.

An **embedding provider abstraction** in code (`musubi.embedding.ProviderInterface` with `.encode_dense`, `.encode_sparse` methods) lets us swap implementations without touching storage code.

## Decision

1. **All vectors in Qdrant are named**, even when there's only one. Default names encode model+version: `dense_<family>_<version>` (e.g., `dense_bge_m3_v1`).
2. **Never pick a bare name like `dense` or `vector`.** Always versioned — future-us will thank us.
3. A Python interface `EmbeddingProvider` with `encode_dense(text) -> list[float]` and `encode_sparse(text) -> dict[int, float]` abstracts the client (TEI HTTP, Gemini, etc.). Config selects which provider produces which named vector.
4. Re-embedding is an *additive* operation ([[11-migration/re-embedding]]): you add a new named vector to existing points, backfill in the background, read in shadow, promote, eventually retire the old one. No destructive migrations.

## Alternatives

**A. Unnamed single vector + separate collections per model.** Works but quadratic: adding a new dense model means a new collection + dual-write everywhere. Loses the "query with multiple embeddings" superpower.

**B. Named vectors but no version suffix.** Fragile — `dense` means different things at different times; a stale client could mis-encode.

**C. Store embeddings outside Qdrant.** No. Qdrant handles them efficiently; external store buys nothing.

**D. Use LangChain/LlamaIndex embedding abstraction.** Too much pulled in. Our abstraction is ~100 lines.

## Consequences

- Any point can have dense+sparse today, and later `dense_bge_m4_v1` added without collection rebuild.
- Config exposes per-named-vector provider + dimension. Boot-time validation checks collection named-vector config matches code.
- Re-embedding path ([[11-migration/re-embedding]]) is exercised for phase 2 (adding sparse) and proves the tooling before we ever *have* to swap a production model.
- Provider abstraction enables local-first (TEI) with a fallback to hosted API per-named-vector if needed.

Trade-offs:

- Per-point payload is slightly larger with multiple vectors. Offset by INT8 scalar quantization on dense. Sparse compresses naturally.
- Developers must remember to specify the named vector on every insert and query. Helper functions centralize this.

## Links

- [[13-decisions/0005-hybrid-search]]
- [[04-data-model/qdrant-layout]]
- [[11-migration/re-embedding]]
- [[06-ingestion/embedding-strategy]]
