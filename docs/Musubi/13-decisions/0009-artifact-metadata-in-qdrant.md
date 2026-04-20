---
title: "ADR 0009: Artifact Metadata Lives in Qdrant, Not a Separate Store"
section: 13-decisions
tags: [adr, artifacts, section/decisions, status/accepted, storage, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0009: Artifact Metadata Lives in Qdrant, Not a Separate Store

**Status:** accepted
**Date:** 2026-03-17
**Deciders:** Eric

## Context

Artifacts (uploaded PDFs, web pages, transcripts) produce:

- **Blobs** — the raw file bytes. Content-addressed by SHA-256. Live on disk at `/var/lib/musubi/artifact-blobs/<sha>`.
- **Chunks** — extracted text broken into retrievable pieces. Live in Qdrant (embedded, queryable).
- **Metadata** — filename, MIME, upload time, source URL, page numbers, chunk relationships.

Where should metadata live? Three options:

1. **In Qdrant payload alongside chunks.** Every chunk carries its parent artifact's metadata in payload.
2. **In a separate sqlite `artifacts` table.** Chunks reference an `artifact_id`; metadata lookup joins against the table.
3. **In the vault (markdown sidecar).** `curated/sources/<slug>.md` with front-matter metadata.

Option 2 adds a store with cross-store joins. Option 3 makes every artifact a curated concern (wrong — an artifact might be purely source, not worth writing up). Option 1 keeps everything in one place, at the cost of some metadata duplication across chunks.

Duplication cost is small: 100-char filename × 20 chunks = 2KB/artifact in extra payload. For our scale this is nothing.

Join cost of option 2 is higher: every chunk retrieval needs a second lookup. And Qdrant is happy to filter on payload fields like `artifact_id` for "all chunks for this artifact" queries.

## Decision

**Artifact metadata lives in Qdrant, in a dedicated `artifact_chunks` collection, redundantly on every chunk payload.**

Collection layout ([[04-data-model/qdrant-layout]]):

- `artifact_chunks` — one point per chunk. Payload includes: `artifact_id`, `blob_sha`, `filename`, `mime`, `source_url`, `chunk_index`, `chunk_count`, `page` (if applicable), plus chunk text.
- Blobs stay on disk (`artifact-blobs/<sha>`). Not in Qdrant.

Lookups:

- Retrieval "find the artifact matching query X" → ANN on `artifact_chunks` → get `artifact_id` + `filename` from payload.
- "Show me all chunks of artifact X" → `scroll` with filter `artifact_id=X`, ordered by `chunk_index`.
- "Fetch the original blob" → read from disk at `artifact-blobs/<blob_sha>`.

No separate `artifacts` table.

## Alternatives

See Context. The rejection of (2) is mostly: joins across a vector store + relational store add a sync story we don't want.

## Consequences

- Deletes are a two-step: remove all chunk points for artifact_id, then delete the blob.
- Updates (re-chunking) are: delete old chunks, insert new chunks (same blob stays).
- Metadata changes (e.g., rename) require updating N chunk payloads. Done via `batch_update_points`. Fine at our scale.

Trade-offs:

- Metadata duplication across chunks. Acceptable; not a real cost.
- No referential integrity guarantee — if we forget to insert chunks after a blob is saved, the blob orphans. Mitigated by a daily "orphan sweep" job ([[06-ingestion/index]]).

## Links

- [[04-data-model/qdrant-layout]]
- [[04-data-model/source-artifact]]
- [[09-operations/asset-matrix]]
