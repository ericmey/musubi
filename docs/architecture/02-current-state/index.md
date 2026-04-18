---
title: Current State
section: 02-current-state
tags: [current-state, gap-analysis, section/current-state, status/complete, type/gap-analysis]
type: gap-analysis
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# 02 — Current State

An honest accounting of what the Musubi POC actually is today (April 2026) and what differs from the target architecture in this vault. The POC is a working v1, not a stub; it informs the migration plan in [[11-migration/index]] but is not a source of truth for the target design.

## Documents in this section

- [[02-current-state/poc-inventory]] — What exists, file-by-file.
- [[02-current-state/gap-analysis]] — Delta from POC to target, per subsystem.
- [[02-current-state/preserved]] — Which POC patterns we carry forward verbatim.

## One-page summary

The POC is:

- A single **FastMCP server** (`mcp_server.py` → `musubi/server.py`) on port 8100, speaking MCP over stdio and streamable-HTTP.
- Two Qdrant collections: `musubi_memories` and `musubi_thoughts`, both 3072-d COSINE dense vectors (Gemini `gemini-embedding-001`), with keyword/float/integer payload indexes.
- Pure-function business logic in `musubi/memory.py` (store/recall/recent/reflect/forget) and `musubi/thoughts.py` (send/check/read/history) — takes `QdrantClient` as first arg, returns `{"status": ...}` or `{"error": ...}` dicts.
- Ingestion-time deduplication at 0.92 cosine similarity (updates existing point's content/tags instead of inserting).
- Access-count tracking (`access_count` + `last_accessed`) updated on recall via batched `set_payload` operations.
- Reflection modes: `summary` (paginated scroll), `stale` (low-access), `frequent` (high-access).
- Per-presence thought read-state tracking (`read_by: list[str]` for broadcasts + global `read: bool` for unicast).
- Single-host `docker-compose.yml` with Qdrant + persistent volume.
- install/update/uninstall shell scripts (Colima assumed on macOS; needs Linux adaptation).
- pytest suite (~90 tests) with `mock_qdrant` + `mock_embed` fixtures; 80% coverage target.
- `make check` — ruff format/lint + mypy strict + pytest+coverage.

The POC works and is in daily use. Do not break it during migration. See [[11-migration/index]] for the phased plan that keeps it running while the target comes online.

## Divergences at a glance

| Area | POC | Target | Notes |
|---|---|---|---|
| Planes | 1 (memories) + thoughts | 3 (episodic, curated, artifact) + concepts + thoughts | [[04-data-model/index]] |
| Vectors | dense only, Gemini remote | dense + sparse, local default | [[05-retrieval/hybrid-search]] |
| Store of record | Qdrant for everything | Obsidian vault for curated; Qdrant for derived | [[06-ingestion/vault-sync]] |
| Interface | MCP-as-server | Canonical API + MCP-as-adapter | [[07-interfaces/index]] |
| Lifecycle | Inline dedup only | Separate worker process | [[06-ingestion/lifecycle-engine]] |
| Inference | Remote (Gemini) | Local (TEI on GPU) | [[08-deployment/gpu-inference-topology]] |
| Deployment | Hand-run scripts | Ansible playbooks | [[08-deployment/ansible-layout]] |
| Versioning | created_at / updated_at only | Full lineage + versioning | [[04-data-model/lifecycle]] |
| Auth | None (localhost MCP) | Bearer tokens + optional mTLS | [[10-security/auth]] |

## What the POC gets right

These patterns are preserved in the target design verbatim:

1. **Pure-function business logic that takes `QdrantClient` as first arg** — keeps testing trivial. Propagated to all new plane modules.
2. **Error-dict return values** (`{"error": "..."}`) — keeps error handling explicit. Upgraded to typed `Result[T, E]` in the target but same philosophy.
3. **Batched `set_payload` updates via `batch_update_points` + `SetPayloadOperation`** — avoids N+1. Carried forward.
4. **Idempotent `ensure_indexes()` on every boot** — lets schema evolve without migrations. Carried forward for all collections.
5. **Context-window-protecting `brief` flag** truncating content by default — kept on all read paths.
6. **Deduplication at ingestion via similarity threshold** — kept, but the threshold becomes per-plane configurable.
7. **Per-presence read-state for broadcasts** (`read_by` + Qdrant `must_not` filter) — preserved as the thoughts channel semantics.

See [[02-current-state/preserved]] for the full list and specific code references.
