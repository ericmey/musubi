---
owner: codex-gpt5-shiori
status: in-review
issue: 493
title: "Slice: STREAM-001 retrieval stream parity"
slice_id: slice-issue493-streaming-retrieval-parity
section: _slices
type: slice
phase: "Retrieval"
tags:
  - section/slices
  - status/in-review
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: STREAM-001 retrieval stream parity

## Context
Fix `POST /v1/retrieve/stream` to use canonical orchestration, returning exact metadata headers instead of mutating the NDJSON payload shape. This addresses drift between standard retrieval and the streaming endpoint.

## Specs to implement
- [[07-interfaces/canonical-api]]

## Owned paths
- `src/musubi/api/routers/writes_retrieve_stream.py`
- `tests/api/test_retrieve_stream.py`

## Forbidden paths
- Core retrieval logic, Qdrant orchestration, TEI/Ollama adapters. No touching C4/DQ/Fresh-memory.

## Test Contract
- `test_streaming_retrieval_ranked`
- `test_streaming_retrieval_recent`
- `test_streaming_retrieval_wildcard_auth_forbids`
- `test_streaming_retrieval_zero_row_warning_header`
- `test_streaming_retrieval_degraded_warning_header`
- `test_streaming_typed_error_mapping`
- `test_streaming_retrieval_forwards_all_query_parameters`
- `test_streaming_retrieval_multi_plane_fanout`

## Definition of Done
- `run_orchestration_retrieve` invoked directly.
- Envelope warnings mapped to `X-Musubi-Warnings` header.
- `make check` full pass.

## Work log
- Initial implementation replacing direct Episodic plane call with orchestration.
- Addressed Yua review: removed unused seed helper, tightened zero-row/degraded assertions, verified setup POST successes, proved parameter forwarding, extended row schema checks, and corrected module docstrings.
- Addressed Aoi independent review: added `test_streaming_retrieval_multi_plane_fanout` and 500 error mapping proof.
