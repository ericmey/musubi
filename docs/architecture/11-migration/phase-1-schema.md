---
title: "Phase 1: Schema"
section: 11-migration
tags: [migration, phase-1, schema, section/migration, status/research-needed, type/migration-phase]
type: migration-phase
status: research-needed
updated: 2026-04-17
up: "[[11-migration/index]]"
next: "[[11-migration/phase-2-hybrid-search]]"
reviewed: false
---
# Phase 1: Schema

Introduce pydantic schemas as the source of truth for all data shapes, without changing storage yet.

## Goal

Every function that touches data — capture, recall, send, check — takes and returns pydantic models rather than raw dicts. The POC uses dicts; we're tightening.

## Why first

- Nothing else works without it. Phase 2's named vectors need to know what a memory looks like. Phase 4's plane split needs per-plane models.
- Low risk — the change is isomorphic to today's payloads.
- Makes the canonical API possible later; FastAPI auto-generates OpenAPI from pydantic.

## Changes

### Add pydantic models

```python
# musubi/models.py  (new)
from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime

class MemoryCreate(BaseModel):
    namespace: str
    content: str = Field(min_length=1, max_length=16000)
    tags: list[str] = []
    topics: list[str] = []
    importance: int = Field(ge=1, le=10, default=5)
    content_type: Literal["observation","fact","preference","todo",...] = "observation"
    capture_source: str
    source_ref: str | None = None
    ingestion_metadata: dict = {}

class Memory(MemoryCreate):
    object_id: str
    state: Literal["provisional","matured","archived"]
    created_epoch: int
    updated_epoch: int
    access_count: int = 0
    last_accessed_epoch: int | None = None
```

Mirror for `ThoughtCreate` / `Thought`.

### Wrap existing functions

```python
# musubi/memory.py
def memory_store(client, payload: dict | MemoryCreate) -> dict:
    if isinstance(payload, dict):
        payload = MemoryCreate.model_validate(payload)
    # existing logic, but use payload.content etc.
```

Dict-in remains supported for backward compatibility; internally we always have a model.

### Update MCP tool schemas

`server.py` sprouts pydantic-derived parameter validation. FastMCP supports this natively — the tool input schema is generated from the pydantic type.

```python
# server.py
@mcp.tool()
def memory_store(
    content: str, namespace: str = "eric/claude-code/episodic",
    tags: list[str] = [], importance: int = 5, ...
) -> dict:
    payload = MemoryCreate(
        content=content, namespace=namespace,
        tags=tags, importance=importance,
        capture_source="claude-code-session"
    )
    return memory.memory_store(qdrant, payload)
```

### Migrate existing data (zero-change)

Existing Qdrant payloads don't match the new model exactly (e.g., no `state`, `capture_source`, `namespace`). Migration:

1. On boot, scan `musubi` collection.
2. For each point missing required fields, set defaults:
   - `state`: `matured`
   - `capture_source`: `legacy`
   - `namespace`: `eric/claude-code/episodic`
   - `object_id`: copy from point id
   - `created_epoch`: copy from `created_epoch` or `updated_epoch`
3. `batch_update_points` with `SetPayloadOperation` in 500-point batches.

Idempotent — safe to re-run.

## Done signal

- All musubi/ modules take/return typed models.
- `make check` passes (ruff, mypy strict, pytest).
- Existing POC clients (Claude Code session) still capture and recall without changes.
- New field `schema_version=1` on every payload.

## Rollback

Revert the code change. Existing Qdrant data already has back-compat defaults from the migration step; nothing to undo there.

## Smoke test

```
pytest tests/test_memory.py
pytest tests/test_thoughts.py
# Claude Code session:
> capture: "hello from phase 1"
> recall: "phase 1"
# Returns the captured memory.
```

## Estimate

~1 week. Mostly schema definition + migration script. Tests update is the largest chunk.

## Pitfalls

- **Don't over-spec enums.** `content_type` tight enum is fine; `tags` as free-form is fine. Don't tighten beyond what today's data supports or the back-fill will break.
- **Watch for string→int coercion on old data.** Some POC payloads stored `importance` as a string. Coerce in the migration pass.
- **Don't remove dict input.** Leave dict support as a thin wrapper; MCP tools often pass dicts from JSON and we want minimal surprise.
