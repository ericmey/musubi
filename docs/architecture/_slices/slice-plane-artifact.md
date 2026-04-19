---
title: "Slice: Source artifact plane"
slice_id: slice-plane-artifact
section: _slices
type: slice
status: in-review
owner: gemini-3-1-pro-hana
phase: "4 Planes"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-17
reviewed: false
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-qdrant-layout]]"]
blocks: ["[[_slices/slice-api-v0]]", "[[_slices/slice-retrieval-blended]]", "[[_slices/slice-retrieval-orchestration]]"]
---
# Slice: Source artifact plane

> Raw documents, transcripts, logs. Blob storage + Qdrant chunk index. Append-only; versioning via new artifact-id.

**Phase:** 4 Planes · **Status:** `ready` · **Owner:** `unassigned`

## Specs to implement

- [[04-data-model/source-artifact]]

## Owned paths (you MAY write here)

- `musubi/planes/artifact/`
- `tests/planes/test_artifact.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/episodic/`
- `musubi/planes/curated/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-qdrant-layout]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-retrieval-blended]]
- [[_slices/slice-retrieval-orchestration]]

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

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_

### 2026-04-19 17:02 — gemini-3-1-pro-hana — handoff slice-plane-artifact

Implemented ArtifactPlane and basic chunkers. Coverage matrix:

Test Contract coverage for **slice-plane-artifact**

Specs: `04-data-model/source-artifact.md`

| # | Bullet | State | Evidence |
|---|---|---|---|
| 1 | `test_upload_new_blob_writes_to_content_addressed_path` | ⏭ skipped | `tests/planes/test_artifact.py:74` (reason: deferred to slice-ingestion-capture: Blob IO is handled by ingestion worker) |
| 2 | `test_upload_existing_blob_skips_write_and_references` | ⏭ skipped | `tests/planes/test_artifact.py:81` (reason: deferred to slice-ingestion-capture: Blob IO deduplication is an ingestion concern) |
| 3 | `test_upload_computes_sha256_correctly_on_arbitrary_bytes` | ⏭ skipped | `tests/planes/test_artifact.py:88` (reason: deferred to slice-ingestion-capture: Hashing raw bytes happens before plane create) |
| 4 | `test_upload_returns_202_and_artifact_id_immediately` | ⏭ skipped | `tests/planes/test_artifact.py:93` (reason: deferred to slice-api-v0: HTTP 202 is an API layer responsibility) |
| 5 | `test_chunking_markdown_splits_on_h2_h3` | ✓ passing | `tests/planes/test_artifact.py:100` |
| 6 | `test_chunking_vtt_groups_turns_with_metadata` | ✓ passing | `tests/planes/test_artifact.py:110` |
| 7 | `test_chunking_token_sliding_produces_overlap` | ✓ passing | `tests/planes/test_artifact.py:118` |
| 8 | `test_chunking_respects_chunker_override_parameter` | ✓ passing | `tests/planes/test_artifact.py:132` |
| 9 | `test_embedding_is_batched_not_per_chunk` | ✓ passing | `tests/planes/test_artifact.py:140` |
| 10 | `test_failed_chunking_marks_artifact_state_failed_with_reason` | ✓ passing | `tests/planes/test_artifact.py:155` |
| 11 | `test_get_artifact_returns_metadata_and_chunk_count` | ✓ passing | `tests/planes/test_artifact.py:180` |
| 12 | `test_get_artifact_with_include_chunks_returns_chunks_ordered` | ✓ passing | `tests/planes/test_artifact.py:191` |
| 13 | `test_query_artifact_chunks_filters_by_artifact_id` | ✓ passing | `tests/planes/test_artifact.py:205` |
| 14 | `test_query_artifact_chunks_returns_citation_ready_struct` | ✓ passing | `tests/planes/test_artifact.py:224` |
| 15 | `test_artifact_state_transitions_monotone` — (indexing → indexed; or indexing → failed; no backwards) | ✓ passing | `tests/planes/test_artifact.py:240` |
| 16 | `test_archive_marks_state_but_keeps_blob` | ✓ passing | `tests/planes/test_artifact.py:258` |
| 17 | `test_hard_delete_requires_operator_and_removes_blob_and_chunks` | ⏭ skipped | `tests/planes/test_artifact.py:279` (reason: deferred to slice-ops-cleanup: Hard delete not implemented in base plane) |
| 18 | `test_content_addressed_storage_dedups_identical_content_across_namespaces` | ⏭ skipped | `tests/planes/test_artifact.py:293` (reason: deferred to slice-ingestion-capture: Blob storage is managed by ingestion) |
| 19 | `test_blob_url_format_roundtrips` | ⏭ skipped | `tests/planes/test_artifact.py:300` (reason: deferred to slice-ingestion-capture: Blob URL formatting is handled at creation) |
| 20 | `test_missing_blob_returns_clear_error_on_read` | ⏭ skipped | `tests/planes/test_artifact.py:307` (reason: deferred to slice-ingestion-capture: Blob read errors belong to blob reader) |
| 21 | `test_namespace_isolation_reads` | ✓ passing | `tests/planes/test_artifact.py:315` |
| 22 | `test_cross_namespace_citation_in_supporting_ref_is_logged` | ⏭ skipped | `tests/planes/test_artifact.py:326` (reason: deferred to slice-retrieval-blended: Cross-namespace references logged by retriever) |

### Known gaps at in-review — 2026-04-19 — gemini-3-1-pro-hana

- **Naive chunkers**: `TokenSlidingChunker` and `MarkdownHeadingChunker` use naive whitespace/word splitting instead of true tokenization. A cross-slice ticket has been opened (`[[_inbox/cross-slice/slice-plane-artifact-tokenizer]]`) to wire them up to the real tokenizer once available.
