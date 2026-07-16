---
title: "Slice: THOUGHT-002 production thought writes use configured embedder"
slice_id: slice-thought002-issue486
issue: 486
section: _slices
type: slice
status: done
owner: codex-gpt5-shiori
phase: "Retrieval"
tags: [section/slices, status/done, type/slice]
updated: 2026-07-15
reviewed: true
depends-on: []
blocks: []
---

# Slice: THOUGHT-002 production thought writes use configured embedder

## What
Replaces explicit `FakeEmbedder()` usage inside the `writes_thoughts.py` API router with the correct `Depends(get_thoughts_plane)` dependency.

## Why
Production routes `/v1/thoughts/send` and `/v1/thoughts/read` were manually instantiating `ThoughtsPlane(client=qdrant, embedder=FakeEmbedder())` instead of using the dependency injection framework, bypassing the production TEI embedder and inserting fake/zero vectors into Qdrant.

## Specs to implement

- [[04-data-model/thoughts]]

## Owned paths

- `src/musubi/api/routers/writes_thoughts.py`
- `tests/api/test_thoughts_writes_plane.py`
- `docs/Musubi/_slices/slice-thought002-issue486.md`

## Forbidden paths

- All other production, test, deployment, and specification paths.

## Test Contract

1. `test_thought_send_uses_configured_plane_and_embedder`
2. `test_thought_read_uses_configured_plane`
3. `test_production_router_has_no_fake_embedder`
4. `test_missing_dependency_fails_loud`

Existing thought API and stream suites must remain green.

## Definition of Done

- [x] `/send` and `/read` use the configured `ThoughtsPlane` dependency.
- [x] Production router code contains no `FakeEmbedder` construction.
- [x] Tests prove nonzero, input-sensitive embedding behavior.
- [x] Missing dependency configuration fails loudly without exposing internals to clients.
- [x] Missing-ID read continuation remains intact.
- [x] Focused and full repository gates pass.
- [x] Independent review completed.

## Work log

### 2026-07-15 — codex-gpt5-shiori — implementation

- Replaced route-local fake plane construction with dependency injection.
- Added behaviorally discriminating send/read, vector, continuation, and failure-boundary tests.

### 2026-07-15 — codex-gpt5-yua — review closeout

- Integrated current main after THOUGHT-001 landed.
- Recorded owned/forbidden paths, the mechanical Test Contract, completion evidence, and independent-review state.
