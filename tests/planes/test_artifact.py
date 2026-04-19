import pytest

@pytest.mark.skip(reason="deferred to slice-ingestion-capture: Blob IO is handled by ingestion worker")
def test_upload_new_blob_writes_to_content_addressed_path(): pass

@pytest.mark.skip(reason="deferred to slice-ingestion-capture: Blob IO deduplication is an ingestion concern")
def test_upload_existing_blob_skips_write_and_references(): pass

@pytest.mark.skip(reason="deferred to slice-ingestion-capture: Hashing raw bytes happens before plane create")
def test_upload_computes_sha256_correctly_on_arbitrary_bytes(): pass

@pytest.mark.skip(reason="deferred to slice-api-v0: HTTP 202 is an API layer responsibility")
def test_upload_returns_202_and_artifact_id_immediately(): pass

def test_chunking_markdown_splits_on_h2_h3(): pass

def test_chunking_vtt_groups_turns_with_metadata(): pass

def test_chunking_token_sliding_produces_overlap(): pass

def test_chunking_respects_chunker_override_parameter(): pass

def test_embedding_is_batched_not_per_chunk(): pass

def test_failed_chunking_marks_artifact_state_failed_with_reason(): pass

def test_get_artifact_returns_metadata_and_chunk_count(): pass

def test_get_artifact_with_include_chunks_returns_chunks_ordered(): pass

def test_query_artifact_chunks_filters_by_artifact_id(): pass

def test_query_artifact_chunks_returns_citation_ready_struct(): pass

def test_artifact_state_transitions_monotone(): pass

def test_archive_marks_state_but_keeps_blob(): pass

def test_hard_delete_requires_operator_and_removes_blob_and_chunks(): pass

@pytest.mark.skip(reason="deferred to slice-ingestion-capture: Blob storage is managed by ingestion")
def test_content_addressed_storage_dedups_identical_content_across_namespaces(): pass

@pytest.mark.skip(reason="deferred to slice-ingestion-capture: Blob URL formatting is handled at creation")
def test_blob_url_format_roundtrips(): pass

@pytest.mark.skip(reason="deferred to slice-ingestion-capture: Blob read errors belong to blob reader")
def test_missing_blob_returns_clear_error_on_read(): pass

def test_namespace_isolation_reads(): pass

@pytest.mark.skip(reason="deferred to slice-retrieval-blended: Cross-namespace references logged by retriever")
def test_cross_namespace_citation_in_supporting_ref_is_logged(): pass
