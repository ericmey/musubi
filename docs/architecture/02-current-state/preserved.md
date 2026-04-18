---
title: Preserved Patterns
section: 02-current-state
tags: [current-state, preserved-patterns, section/current-state, status/complete, type/gap-analysis]
type: gap-analysis
status: complete
updated: 2026-04-17
up: "[[02-current-state/index]]"
reviewed: false
---
# Preserved Patterns

Patterns from the POC carried forward verbatim into the target. Coding agents should reuse these exactly, not reinvent them.

## 1. Pure-function business logic

**From:** `musubi/memory.py` — all functions take `QdrantClient` as first arg, no MCP dependencies.

**Preserved because:** makes testing trivial. Mock `QdrantClient`, done.

**Extended to:** every plane module (`musubi/planes/*/`), every lifecycle job, every retrieval function.

```python
# Target shape (preserved from POC):
def episodic_store(
    client: QdrantClient,
    *,
    content: str,
    presence: str,
    tenant: str,
    tags: list[str] | None = None,
    ...
) -> Result[StoreResult, StoreError]:
    ...
```

## 2. Error dict → Result[T, E]

**From:** `{"error": "..."}` return dicts.

**Preserved philosophy:** errors are values, not exceptions. Caller checks explicitly.

**Upgraded form:**
```python
from dataclasses import dataclass
from typing import Generic, TypeVar, Union

T = TypeVar("T"); E = TypeVar("E")

@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T

@dataclass(frozen=True)
class Err(Generic[E]):
    error: E

Result = Union[Ok[T], Err[E]]
```

Error dataclasses live in each module next to the functions that return them.

## 3. Batched payload updates

**From:** `memory.memory_recall` uses `batch_update_points(collection, [UpdateOperation...])` to increment `access_count` on all returned memories in one call, not N calls.

**Preserved verbatim.** Every access-counter update, tag merge, or maturation state transition goes through `batch_update_points`. Looping `set_payload` is a lint-level anti-pattern.

## 4. Idempotent index creation

**From:** `collections._ensure_indexes` runs on every boot, is safe to call repeatedly, and creates missing indexes only.

**Preserved.** Same pattern for all new collections. Index declarations live next to collection declarations in `musubi/planes/<plane>/collection.py`.

## 5. Similarity-threshold deduplication

**From:** `memory_store` — queries for nearest neighbor; if ≥ 0.92 cosine similarity, updates existing point instead of inserting.

**Preserved,** with modifications:
- The threshold is plane-specific (`EPISODIC_DEDUP_THRESHOLD`, `CURATED_DEDUP_THRESHOLD`, etc.).
- Updates merge tags (union) and preserve the highest importance score, rather than overwriting.
- Updates bump `updated_at` / `updated_epoch` and increment `reinforcement_count`.
- A rejected-duplicate is *logged* with the incoming id → existing id mapping so consumers can trace "why did my memory not appear?"

## 6. `brief` parameter on all read paths

**From:** `memory_recall(..., brief=True)` truncates `content` to ~200–300 chars in results to protect the caller's context window.

**Preserved.** Default `brief=True` on all read paths. Full content always available by querying specific IDs.

## 7. `scroll()` with `next_page_offset` for unbounded aggregation

**From:** `memory_reflect` paginates via Qdrant's `scroll` API.

**Preserved.** Every aggregation job in the Lifecycle Engine paginates. Never assume a single scroll returns everything.

## 8. Qdrant-side filtering

**From:** `thought_check` uses `must_not` filter on `read_by` containing the caller, plus `must_not` on `from_presence == caller`, so `limit` is respected correctly.

**Preserved as a rule:** any filter you can express in Qdrant lives in Qdrant. Python-side filtering breaks pagination and `limit`.

## 9. Per-presence read tracking (thoughts)

**From:** `read: bool` (global) + `read_by: list[str]` (per-presence). `thought_check` filters by `read_by NOT CONTAINS my_presence`.

**Preserved verbatim** for the thoughts channel. Extended to curated-knowledge read-state in the post-v1 roadmap.

## 10. `updated_epoch` for dedup visibility on recent queries

**From:** `memory_recent` uses a `should` filter (OR) on both `created_epoch` and `updated_epoch` so memories refreshed by dedup still appear in "recent."

**Preserved** and generalized: any time a memory is touched (accessed, merged, reinforced, promoted), we bump `updated_epoch`. Recent-query semantics are "things that happened — including metadata events — recently."

## 11. Configuration as the single source of truth

**From:** `musubi/config.py` — all env + constants. Nothing hardcoded elsewhere.

**Preserved strictly.** Guarded by a lint rule (custom ruff check) that flags env reads outside `config.py`.

## 12. `conftest.py` fixtures

**From:** `mock_qdrant`, `mock_embed`, `FakePoint`, `FakeQueryResult`.

**Preserved and extended** with `mock_tei` (for local embedding service), `mock_ollama`, `mock_vault` (for filesystem operations), and additional fakes for named-vector responses.

## Checklist for a new coding agent

Before opening a PR, verify:

- [ ] Business logic functions take their dependencies (`client`, etc.) as explicit args, not module-level singletons.
- [ ] Errors return `Err(<TypedError>)`, not raise.
- [ ] Any multi-point write uses `batch_update_points`.
- [ ] Filters live in Qdrant queries, not Python.
- [ ] Indexes are declared in the plane's `collection.py` and created by an idempotent `ensure_*` function.
- [ ] New config values live in `musubi/config.py` with a default and a type.
- [ ] Tests use the standard fixtures.
- [ ] `brief=True` is default on read paths that return `content`.
