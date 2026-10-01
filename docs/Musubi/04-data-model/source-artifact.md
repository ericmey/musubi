---
title: Source Artifact
section: 04-data-model
tags: [artifact, data-model, schema, section/data-model, status/draft, type/spec]
type: spec
status: draft
updated: 2026-10-01
up: "[[04-data-model/index]]"
reviewed: false
implements: "tests/planes/test_artifact.py"
---
# Source Artifact

Raw, immutable material. Transcripts, documents, channel exports, whatever needs to be the ground truth behind a citation.

## Pydantic models

Both models are in `src/musubi/types/artifact.py`. `SourceArtifact` (`artifact.py:17-82`) extends `MusubiObject`, so it inherits `object_id`, `namespace` (e.g. `alex/shared/artifact`), `identity_family`, `schema_version`, the `created_*` / `updated_*` timestamps and `version`. It is not a `MemoryObject`: it has no lineage, tags, importance or validity fields.

```python
class SourceArtifact(MusubiObject):
    state: Literal["matured", "archived", "superseded"] = "matured"   # lifecycle axis
    title: str                          # non-empty
    filename: str                       # non-empty
    sha256: str                         # 64-char hex of the raw bytes
    content_type: str                   # MIME
    size_bytes: int
    chunk_count: int = 0
    ingestion_metadata: dict = {}       # e.g. {"source_system": "api-upload"}
    chunker: str                        # e.g. "markdown-headings-v1"
    artifact_state: Literal["indexing", "indexed", "failed", "stored_unindexed"] = "indexing"
    failure_reason: str | None = None

    # Committed-generation head (C4 / ART-001)
    committed_generation: str | None = None
    committed_owner: str | None = None
    index_operation_id: str | None = None
    publication_version: int = 0
```

Validators: `failed` requires `failure_reason`; `indexed` requires `chunk_count ≥ 1`; `stored_unindexed` forbids every indexing-owned field. A reader exposes only chunks whose `(generation, owner_token)` equal the head's committed pair.

```python
class ArtifactChunk(BaseModel):          # artifact.py:85-107, frozen
    chunk_id: KSUID
    artifact_id: KSUID
    chunk_index: int
    content: str
    start_offset: int
    end_offset: int                      # >= start_offset
    chunk_metadata: dict = {}
    generation: str | None = None        # staging fence; None on legacy chunks
    owner_token: str | None = None
    # Stored as a Qdrant point in musubi_artifact_chunks
```

Chunks are not first-class `MusubiObject`s — they're indexed content owned by the parent artifact. Lifecycle of a chunk == lifecycle of its parent.

There are no `source_system`, `source_ref`, `ingested_by`, `blob_url`, `derived_from` or `supersedes` fields on the model. The upload route records the caller's `source_system` (default `"api-upload"`) inside `ingestion_metadata`.

## Storage layout

**Blob:** one file per artifact at `ARTIFACT_BLOB_PATH/<namespace>/<object_id>` (default root `/var/lib/musubi/artifact-blobs`, `.env.example:37`; `src/musubi/api/routers/writes_artifact.py:105-111`, `src/musubi/api/routers/artifacts.py:75-90`). Uploads stream to `ARTIFACT_BLOB_PATH/.staging/` first and are moved into place. There is no content addressing: two uploads of identical bytes are two blobs. Content-addressed or object-store blob storage is planned, not implemented.

**Qdrant:** collection `musubi_artifact_chunks` stores chunk embeddings, with the universal indexes plus `artifact_id`, `chunk_id`, `chunk_index`, `content_type`, `chunker`, `source_system` (`src/musubi/store/specs.py:196-203`). Vectors: same named vectors as other planes (`dense_bge_m3_v1`, `sparse_splade_v1`).

## Chunking strategies

| Chunker | For | Approach |
|---|---|---|
| `markdown-headings-v1` | `.md`, `.txt` with headings | Split on H2/H3; token-split a section that exceeds the 512-token window; heading path in `chunk_metadata.heading_path`. |
| `vtt-turns-v1` | `.vtt`, `.srt` | Group 3–5 blank-line-separated turns per chunk; metadata: `speakers`. |
| `token-sliding-v1` | default | 512-token window, 128-token overlap; BGE-M3 tokenizer. |
| `json-v1` | `.json` export | One chunk per top-level array element; unparseable JSON becomes one chunk. |

The chunker is not inferred from the content type: the upload's `chunker` form field selects it and defaults to `markdown-headings-v1` (`writes_artifact.py:55`). The registry is `KNOWN_CHUNKERS` in `src/musubi/planes/artifact/chunking.py:336`; the committed-generation indexer rejects an unknown name.

## Ingestion flow

```
POST /v1/artifacts   (multipart form: namespace, title, content_type,
                      source_system?, chunker?, file)
  │
  ▼
Core:
  1. auth: write scope on the form's namespace
  2. stream bytes to a staging file, hashing (sha256) and refusing past ARTIFACT_MAX_BYTES
  3. create the SourceArtifact head (artifact_state="indexing") in musubi_artifact
  4. move the blob to ARTIFACT_BLOB_PATH/<namespace>/<object_id>
  5. enqueue a durable indexing intent; at capacity, mark the artifact failed instead
  6. return 202 with object_id, state (the indexing axis), size_bytes, sha256

Lifecycle Worker (ArtifactIndexer):
  1. open blob
  2. chunk per the named chunker
  3. batch-embed dense + sparse via TEI
  4. stage the chunks under a fresh generation in musubi_artifact_chunks
  5. publish the head: committed_generation/owner, artifact_state="indexed", chunk_count=N
```

Client polls `GET /v1/artifacts/{id}` to see state transition `indexing` → `indexed` / `failed`. Other routes: `GET /v1/artifacts` (list), `GET /v1/artifacts/{id}/chunks`, `GET /v1/artifacts/{id}/blob`, `POST /v1/artifacts/{id}/archive` (write scope; lifecycle `archived`), `POST /v1/artifacts/{id}/purge` (operator scope; removes metadata and blob).

`stored_unindexed` is a separate, intentional branch for an artifact whose blob
is retained for exact by-id reads but never admitted to chunk indexing. Its head
must have `chunk_count=0` and no `committed_generation`, `committed_owner`,
`index_operation_id`, or `failure_reason`. The ART-004 escrow writer owns the
production creation path, synthetic title/filename policy, initial
`publication_version=0`, suppression of indexing intents, and the remaining
legacy `ArtifactPlane.index()` entry point. ART-004 also proves semantic-search
absence with an indexed positive control; ART-003 proves only the strict model
shape and by-id readability with zero committed chunks.

## Where artifact metadata lives

Artifact metadata is a Qdrant point in the `musubi_artifact` collection — no second store. The point is written with an all-zero dense vector (`src/musubi/planes/artifact/plane.py:133-141`), so metadata is reached by payload filter, not by similarity; the collection has no sparse vector (`src/musubi/store/specs.py:104`). Embedding title + summary for search-by-artifact is planned, not implemented. See [[13-decisions/0009-artifact-metadata-in-qdrant]].

The `musubi_artifact` collection also declares payload indexes on `source_system`, `source_ref`, `ingested_by` and `derived_from` (`specs.py:205-212`). Those are not model fields and nothing writes them today, so filtering on them matches nothing.

## Test Contract

**Module under test:** `src/musubi/planes/artifact/` + `src/musubi/store/`

Behaviour checklist; implemented tests are in `tests/planes/test_artifact.py` and neighbours under their own names.

Ingestion:

1. `test_upload_writes_blob_under_namespace_and_object_id`
2. `test_upload_computes_sha256_correctly_on_arbitrary_bytes`
3. `test_upload_returns_202_and_artifact_id_immediately`
4. `test_chunking_markdown_splits_on_h2_h3`
5. `test_chunking_vtt_groups_turns_with_metadata`
6. `test_chunking_token_sliding_produces_overlap`
7. `test_chunking_respects_chunker_override_parameter`
8. `test_embedding_is_batched_not_per_chunk`
9. `test_failed_chunking_marks_artifact_state_failed_with_reason`

Query:

10. `test_get_artifact_returns_metadata_and_chunk_count`
11. `test_get_artifact_with_include_chunks_returns_chunks_ordered`
12. `test_query_artifact_chunks_filters_by_artifact_id`
13. `test_query_artifact_chunks_returns_citation_ready_struct`

Lifecycle:

14. `test_artifact_state_transitions_monotone` (indexing → indexed; or indexing → failed; no backwards)
15. `test_archive_marks_state_but_keeps_blob`
16. `test_hard_delete_requires_operator_and_removes_blob_and_chunks`

Storage:

17. `test_missing_blob_returns_clear_error_on_read`

Stored-unindexed state:

18. `test_stored_unindexed_accepts_only_empty_indexing_state`
19. `test_stored_unindexed_rejects_every_indexing_owned_field`
20. `test_stored_unindexed_artifact_is_readable_by_id_with_zero_committed_chunks`
21. `test_escrow_id_matches_adr_golden_vector`
22. `test_escrow_id_binds_namespace_source_and_digest_while_preserving_timestamp`
23. `test_escrow_temp_fsync_failure_exposes_no_final_or_head`
24. `test_escrow_blob_readback_failure_exposes_no_head`
25. `test_escrow_head_failure_retry_reuses_verified_bytes_at_version_zero`
26. `test_escrow_corrupt_final_blob_fails_closed_without_overwrite`
27. `test_existing_divergent_escrow_head_fails_closed`
28. `test_concurrent_identical_escrows_converge_on_one_blob_and_head`
29. `test_verified_escrow_is_readable_with_zero_chunks_and_no_intent`
30. `test_escrow_exact_text_search_misses_with_indexed_positive_control`
31. `test_legacy_index_door_refuses_live_stored_head_from_stale_caller`
32. `test_retention_refuses_stored_unindexed_artifact_policy_candidate`
33. `test_real_storage_escrow_orders_verified_blob_before_head_and_reuses`

Isolation:

34. `test_namespace_isolation_reads`
35. `test_cross_namespace_citation_in_supporting_ref_is_logged`

## Prior art

- Mem0 artifact pattern (documents → facts): [https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)
- Chunking heuristics: [https://qdrant.tech/articles/sparse-vectors/](https://qdrant.tech/articles/sparse-vectors/), LlamaIndex node parsers.

## Open questions

- **PDFs and OCR:** there is no PDF text extraction and no OCR. Chunkers read the blob as text. Text extraction (and a local OCR worker for image-only PDFs) is planned, not implemented.
- **Audio artifacts:** v1 expects a pre-transcribed VTT/SRT. We don't ship a speech-to-text pipeline. That's the adapter's job (the LiveKit adapter captures the transcript; Musubi ingests it).
- **Multi-part artifacts** (e.g., a PDF + companion spreadsheet): v1 = one artifact per file. Use `derived_from` to link. Post-v1: artifact collections.
