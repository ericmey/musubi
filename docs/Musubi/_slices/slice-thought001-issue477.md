---
title: "Slice: THOUGHT-001 check/history presence filtering"
slice_id: slice-thought001-issue477
issue: 477
section: _slices
type: slice
status: in-review
owner: codex-gpt5-shiori
phase: "Retrieval"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-07-14
reviewed: true
depends-on: []
blocks: []
---

# Slice: THOUGHT-001 check/history presence filtering

## What
Fixes `/v1/thoughts/check` and `/v1/thoughts/history` to enforce presence filtering according to their respective semantics.

## Why
Currently these routes only filter by `namespace` and ignore the required `presence` body argument, allowing callers to view unrelated thoughts.

## Specs to implement

- [[04-data-model/thoughts]]

## Owned paths

- `src/musubi/api/routers/thoughts.py`
- `src/musubi/planes/thoughts/plane.py`
- `tests/api/test_thoughts_check_history.py`
- `openapi.yaml`
- `docs/Musubi/_slices/slice-thought001-issue477.md`

## Forbidden paths

- All other production, test, deployment, and specification paths.

## Test Contract

1. `test_check_includes_unicast_and_broadcast_excludes_unrelated`
2. `test_history_includes_sent_and_received_excludes_unrelated`
3. `test_namespace_auth_enforced_before_read`

Existing thought stream and write suites must remain green.

## Definition of Done

- [x] `/check` enforces unread presence semantics.
- [x] `/history` returns only sent, received, and broadcast thoughts for the requested presence.
- [x] Namespace authorization remains enforced.
- [x] Runtime OpenAPI and the committed snapshot remain equal.
- [x] Focused and full repository gates pass.
- [x] Independent review completed.

## Work log

### 2026-07-14 — codex-gpt5-shiori — implementation

- Added presence-filtering regressions for `/check` and `/history` and preserved namespace authorization coverage.
- Routed both endpoints through `ThoughtsPlane` presence-aware operations.

### 2026-07-15 — codex-gpt5-yua — review closeout

- Corrected route documentation and refreshed the matching OpenAPI snapshot.
- Removed the API test's dependency on the plane-private point-id helper.
- Recorded owned/forbidden paths, the mechanical Test Contract, completion evidence, and independent-review state.
