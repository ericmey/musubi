---
title: "Slice: Retrieve mode=recent"
slice_id: slice-retrieve-recent
section: _slices
type: slice
status: blocked
phase: "8 Post-1.0"
tags: [section/slices, status/blocked, type/slice, api, retrieve, recency]
updated: 2026-04-29
reviewed: false
depends-on: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-retrieve-wildcards]]"]
blocks: ["[[_slices/slice-mcp-canonical-tools]]", "[[_slices/slice-livekit-canonical-tools]]", "[[_slices/slice-openclaw-canonical-tools]]"]
---

# Slice: Retrieve mode=recent

> Add `mode=recent` to `POST /v1/retrieve` — query-less, time-ordered scroll across the namespace fanout. Required by the canonical `musubi_recent` agent tool ([[07-interfaces/agent-tools]]) so an agent can answer "what was I just doing?" without a query string.

**Phase:** 8 Post-1.0 · **Status:** `blocked` (on [[_slices/slice-api-retrieve-wildcards]])

## Why this slice exists

Today's retrieve modes (`fast`, `deep`, `blended`) all require `query_text` and rank by similarity-with-recency-weight. They cannot answer "give me the last N items, no query" — recency-only retrieval has no path. The voice-active path (`openclaw-livekit`) works around this by paginating `GET /v1/episodic`, but that surface is per-namespace and Qdrant-scroll-ordered (not strictly newest-first) and cannot fan out across modalities the way `POST /v1/retrieve` can with wildcard namespaces ([[13-decisions/0031-retrieve-wildcard-namespace]]).

The canonical agent-tools surface ([[13-decisions/0032-agent-tools-canonical-surface]]) makes `musubi_recent` cross-modal by default. That requires a backend recency endpoint that fans out across `<tenant>/*/episodic`. This slice adds it.

## Specs to implement

- [[07-interfaces/agent-tools]] (the consumer; defines the contract `musubi_recent` must satisfy)
- [[07-interfaces/canonical-api]] (spec-update trailer: extend §Retrieve modes table to include `recent`)
- [[13-decisions/0031-retrieve-wildcard-namespace]] (no change; relied on for cross-modal fanout)
- [[13-decisions/0032-agent-tools-canonical-surface]] (the decision motivating this slice)

## Owned paths

This slice is `blocked` on [[_slices/slice-api-retrieve-wildcards]] (currently `in-progress`). The files this slice will touch are mostly owned by that active slice and **MUST NOT** be claimed here while it is in flight — see the "Anticipated paths at pickup" section below. When this slice transitions to `in-progress`, the slice author moves the anticipated-paths list into this section.

For now, this slice claims **no active paths** — it's a tracking spec, not a worker.

## Anticipated paths at pickup

The slice author who claims this slice (after [[_slices/slice-api-retrieve-wildcards]] merges) will add these into ## Owned paths:

Owned outright:

- src/musubi/retrieve/orchestration.py
- tests/retrieve/test_orchestration.py
- openapi.yaml
- docs/Musubi/07-interfaces/canonical-api.md

Released by [[_slices/slice-api-retrieve-wildcards]] when it merges:

- src/musubi/api/routers/retrieve.py
- src/musubi/sdk/async_client.py
- src/musubi/sdk/client.py
- tests/api/test_retrieve_router.py
- tests/sdk/test_async_client.py

## Forbidden paths

- `src/musubi/types/` — no new types needed; existing `RetrieveQuery` extends additively
- `src/musubi/embedding/` — recent mode does not invoke embedders
- `src/musubi/retrieve/scoring.py` — recency weight already exists; no scoring change

## Depends on

- [[_slices/slice-api-v0-read]] — base retrieve infrastructure
- [[_slices/slice-api-retrieve-wildcards]] — provides the `<tenant>/*/episodic` fanout `musubi_recent` consumes

## Unblocks

- [[_slices/slice-mcp-canonical-tools]]
- [[_slices/slice-livekit-canonical-tools]]
- [[_slices/slice-openclaw-canonical-tools]]

## Test Contract

- [ ] `POST /v1/retrieve` with `mode="recent"` and no `query_text` returns 200 (today returns 422).
- [ ] Results are ordered by `created_at` descending. The first row is the most recently captured.
- [ ] `since` filters to rows with `created_at >= since`. Rows older than `since` are excluded.
- [ ] `tags` filter (already on retrieve) composes with `mode=recent` — only rows whose `tags` contain every listed tag are returned.
- [ ] Cross-modal fanout: with `<tenant>/*/episodic` namespace, results include rows from every modality the tenant has written to. Each row's response carries its concrete stored namespace.
- [ ] Plane filter narrows: `planes=["episodic"]` (default) returns episodic only. Reasonable extension to `["artifact"]` for future use.
- [ ] Limit cap: `limit > 50` is clamped to 50 (or the existing global cap).
- [ ] `mode="recent"` + `query_text` → either accept-and-ignore or 422 — TBD by reviewer; spec the choice.
- [ ] No embedder call. Tracing/observability counters confirm zero embed-cache traffic per request.
- [ ] No rerank call. Same observability check.
- [ ] Latency budget: `mode=recent` p99 ≤ 200ms on a populated dev namespace.

## Definition of Done

![[00-index/definition-of-done]]
