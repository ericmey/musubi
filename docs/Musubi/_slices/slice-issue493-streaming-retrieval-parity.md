---
owner: codex-gpt5-shiori
status: active
issue: "#493"
---
# Streaming Retrieval Parity

## Context
Fix `POST /v1/retrieve/stream` to use canonical orchestration, returning exact metadata headers instead of mutating the NDJSON payload shape. This addresses drift between standard retrieval and the streaming endpoint.

## Scope
- **Owned Paths:** `src/musubi/api/routers/writes_retrieve_stream.py`, `tests/api/test_retrieve_stream.py`
- **Forbidden Paths:** Core retrieval logic, Qdrant orchestration, TEI/Ollama adapters. No touching C4/DQ/Fresh-memory.
- **Specs:** `docs/Musubi/07-interfaces/canonical-api.md`, `docs/Musubi/05-retrieval/retrieval.md`

## Test Contract
- Exact header bounds for `X-Musubi-Warnings` (`[]` or exact codes).
- Wildcard resolution fanout auth validation.
- Query filter forwarding (`state_filter`, `since`, `tags`, etc.).
- Row schema fidelity (ranked vs recent extra blocks).

## Definition of Done
- `run_orchestration_retrieve` invoked directly.
- Envelope warnings mapped to `X-Musubi-Warnings` header.
- `make check` full pass.

## Work Log
- Initial implementation replacing direct Episodic plane call with orchestration.
- Addressed Yua review: removed unused seed helper, tightened zero-row/degraded assertions, verified setup POST successes, proved parameter forwarding, extended row schema checks, and corrected module docstrings.
