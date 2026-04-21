---
title: Source Artifact
section: 04-data-model
tags: [artifact, data-model, schema, section/data-model, status/draft, type/spec]
type: spec
status: draft
updated: 2026-04-17
up: "[[04-data-model/index]]"
reviewed: false
implements: "tests/planes/test_artifact.py"
---
# Source Artifact

Raw, immutable material. Transcripts, documents, channel exports, whatever needs to be the ground truth behind a citation.

## Pydantic models

```python
# musubi/types/artifact.py

ArtifactState = Literal["indexing", "indexed", "failed", "archived"]
Chunker = Literal["markdown-headings-v1", "vtt-turns-v1", "token-sliding-v1", "json-v1"]

class SourceArtifact(BaseModel):
    object_id: KSUID
    namespace: str                      # e.g., "eric/_shared/artifact"
    schema_version: int = 1

    title: str
    filename: str
    sha256: str                         # blob content hash
    content_type: str                   # MIME
    size_bytes: int

    # Chunking
    chunker: Chunker
    chunk_count: int
    chunker_config: dict = Field(default_factory=dict)

    # Temporal
    created_at: datetime
    created_epoch: float
    updated_at: datetime                 # = created_at in almost all cases
    updated_epoch: float

    # Lifecycle
    version: int = 1
    state: LifecycleState = "matured"    # artifacts skip provisional; they're always final
    artifact_state: ArtifactState = "indexing"
    failure_reason: str | None = None

    # Ingestion metadata
    source_system: str                   # "livekit-session", "claude-code-session", "manual-upload", "discord-export", etc.
    source_ref: str | None = None        # URL / session id / message id
    ingested_by: str                     # presence that uploaded
    ingestion_metadata: dict = Field(default_factory=dict)

    # Storage
    blob_url: str                        # internal URL: file:///srv/musubi/artifacts/<sha256[:2]>/<sha256[2:]>/<filename>

    # Relationships (rare for artifacts)
    derived_from: KSUID | None = None    # e.g., a summarized artifact pointing to the raw one
    supersedes: list[KSUID] = Field(default_factory=list)  # rare: explicitly-replaced artifact version
```

```python
class ArtifactChunk(BaseModel):
    chunk_id: KSUID
    artifact_id: KSUID
    chunk_index: int
    content: str
    start_offset: int
    end_offset: int
    chunk_metadata: dict = Field(default_factory=dict)
    # Stored as a Qdrant point in musubi_artifact_chunks
```

Chunks are not first-class `MusubiObject`s — they're indexed content owned by the parent artifact. Lifecycle of a chunk == lifecycle of its parent.

## Storage layout

**Blob:** content-addressed filesystem (v1):

```
/srv/musubi/artifacts/
├── ab/
│   └── cd1234...ffffffff/     # sha256[:2] / sha256[2:]
│       ├── 20260417-session.vtt   # original filename preserved inside the dir
│       └── metadata.json          # ingestion-time metadata snapshot
```

Two artifacts with the same content (identical sha256) share the blob and can have independent `object_id`s (e.g., ingested under different namespaces or with different metadata). Metadata is deduplicated by object_id.

Future (when multi-host): swap filesystem for MinIO without changing the API surface.

**Qdrant:** collection `musubi_artifact_chunks` stores chunk embeddings.

| Field | Type | Purpose |
|---|---|---|
| `namespace` | KEYWORD | scope |
| `artifact_id` | KEYWORD | reverse-join to parent |
| `chunk_id` | KEYWORD | direct lookup |
| `chunk_index` | INTEGER | ordering |
| `content_type` | KEYWORD | filter by MIME |
| `chunker` | KEYWORD | tooling compat |
| `source_system` | KEYWORD | provenance filter |
| `created_epoch` | FLOAT | recency |

Vectors: same named vectors as other planes (`dense_bge_m3_v1`, `sparse_splade_v1`).

## Chunking strategies

| Chunker | For | Approach |
|---|---|---|
| `markdown-headings-v1` | `.md`, `.txt` with headings | Split on H2/H3; fall back to token-sliding if sections > 2048 tokens; preserve heading path in `chunk_metadata.heading_path`. |
| `vtt-turns-v1` | `.vtt`, `.srt` | Group 3–5 speaker turns per chunk; metadata: `speakers`, `start_ts`, `end_ts`. |
| `token-sliding-v1` | default | 512-token window, 128-token overlap; BGE-M3 tokenizer. |
| `json-v1` | `.json` export | One chunk per top-level array element up to 2KB; preserves JSONPath. |

Chunker is selected by content-type + heuristics. Users can override via `chunker` parameter on POST.

## Ingestion flow

```
POST /v1/artifacts   (multipart: metadata json + file bytes OR pre-signed ref)
  │
  ▼
Core:
  1. auth, validate
  2. compute sha256 of bytes
  3. if blob already exists at content-address: skip write
     else: stream to /srv/musubi/artifacts/<ab>/<cd...>/
  4. create SourceArtifact row (in-memory + Qdrant metadata collection — see below)
  5. return 202 Accepted, artifact_id, state:"indexing"
  6. enqueue chunking job (in-process task group; the worker handles the CPU/GPU-heavy parts)

Chunking worker (inside Core or Lifecycle Worker):
  1. open blob
  2. chunk per selected strategy
  3. batch-embed dense + sparse via TEI
  4. upsert chunks into musubi_artifact_chunks
  5. update artifact: artifact_state="indexed", chunk_count=N
```

Client polls `GET /v1/artifacts/{id}` to see state transition `indexing` → `indexed` / `failed`.

## Where artifact metadata lives

We have two options:
- **A: In a SQLite/Postgres metadata table.** Classical.
- **B: As a Qdrant point in a metadata collection (`musubi_artifacts`).** No second store.

**Choice: B for v1.** Keeps the number of stores down. Payload fields are indexed. The artifact metadata point has no vector embedding — we create it with a dummy zero vector (or use Qdrant's upcoming metadata-only points feature if available in 1.15+). See [[13-decisions/0009-artifact-metadata-in-qdrant]].

If Qdrant zero-vector storage is awkward, we embed `title + summary` with BGE-M3 and get free search-by-artifact for free — arguably useful.

## Test Contract

**Module under test:** `musubi/planes/artifact/` + `musubi/store/`

Ingestion:

1. `test_upload_new_blob_writes_to_content_addressed_path`
2. `test_upload_existing_blob_skips_write_and_references`
3. `test_upload_computes_sha256_correctly_on_arbitrary_bytes`
4. `test_upload_returns_202_and_artifact_id_immediately`
5. `test_chunking_markdown_splits_on_h2_h3`
6. `test_chunking_vtt_groups_turns_with_metadata`
7. `test_chunking_token_sliding_produces_overlap`
8. `test_chunking_respects_chunker_override_parameter`
9. `test_embedding_is_batched_not_per_chunk`
10. `test_failed_chunking_marks_artifact_state_failed_with_reason`

Query:

11. `test_get_artifact_returns_metadata_and_chunk_count`
12. `test_get_artifact_with_include_chunks_returns_chunks_ordered`
13. `test_query_artifact_chunks_filters_by_artifact_id`
14. `test_query_artifact_chunks_returns_citation_ready_struct`

Lifecycle:

15. `test_artifact_state_transitions_monotone` (indexing → indexed; or indexing → failed; no backwards)
16. `test_archive_marks_state_but_keeps_blob`
17. `test_hard_delete_requires_operator_and_removes_blob_and_chunks`

Storage:

18. `test_content_addressed_storage_dedups_identical_content_across_namespaces`
19. `test_blob_url_format_roundtrips`
20. `test_missing_blob_returns_clear_error_on_read`

Isolation:

21. `test_namespace_isolation_reads`
22. `test_cross_namespace_citation_in_supporting_ref_is_logged`

## Prior art

- Mem0 artifact pattern (documents → facts): [https://arxiv.org/abs/2504.19413](https://arxiv.org/abs/2504.19413)
- Chunking heuristics: [https://qdrant.tech/articles/sparse-vectors/](https://qdrant.tech/articles/sparse-vectors/), LlamaIndex node parsers.

## Open questions

- **OCR for image-bearing PDFs:** v1 uses `pdfminer.six` for text extraction; no OCR. If a PDF is image-only, ingestion fails fast with a clear error. Post-v1: add a local OCR worker (Tesseract or TrOCR on GPU).
- **Audio artifacts:** v1 expects a pre-transcribed VTT/SRT. We don't ship a speech-to-text pipeline. That's the adapter's job (LiveKit adapter captures transcript; we ingest it).
- **Multi-part artifacts** (e.g., a PDF + companion spreadsheet): v1 = one artifact per file. Use `derived_from` to link. Post-v1: artifact collections.
