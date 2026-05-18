---
title: "Slice: Retrieve mode=recent"
slice_id: slice-retrieve-recent
section: _slices
type: slice
status: in-progress
owner: aoi-claude-opus
phase: "8 Post-1.0"
tags: [section/slices, status/in-progress, type/slice, api, retrieve, recency]
updated: 2026-05-17
reviewed: false
depends-on: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-retrieve-wildcards]]"]
blocks: ["[[_slices/slice-mcp-canonical-tools]]", "[[_slices/slice-livekit-canonical-tools]]", "[[_slices/slice-openclaw-canonical-tools]]"]
---

# Slice: Retrieve mode=recent

> Add `mode=recent` to `POST /v1/retrieve` — query-less, time-ordered scroll across the namespace fanout. Required by the canonical `musubi_recent` agent tool ([[07-interfaces/agent-tools]]) so an agent can answer "what was I just doing?" without a query string.

**Phase:** 8 Post-1.0 · **Status:** `in-progress` (picked up 2026-05-17 — `slice-api-retrieve-wildcards` merged 2026-04-24 via PR #268; the tracker had stayed `blocked` for ~3 weeks past the actual unblock)

## Design decisions (locked at pickup, 2026-05-17)

- **`query_text` provided with `mode="recent"`:** accept-and-ignore with a WARN-level log. Voice MCP callers pass stray context; 422 creates noise the assistant has to apologize about. Strict can be added later; lenient is hard to back out of.
- **`since` shape:** `float` (epoch seconds) only. Canonical internal type. ISO conversion is one line client-side.
- **Default `state_filter` for `mode="recent"`:** `("provisional", "matured", "promoted")` — deliberately includes `provisional` (different from fast/deep defaults of `("matured", "promoted")`). The mode's purpose is "what just happened" and provisional is the freshest tier; excluding it defeats the use case.

## Why this slice exists

Today's retrieve modes (`fast`, `deep`, `blended`) all require `query_text` and rank by similarity-with-recency-weight. They cannot answer "give me the last N items, no query" — recency-only retrieval has no path. The voice-active path (`openclaw-livekit`) works around this by paginating `GET /v1/episodic`, but that surface is per-namespace and Qdrant-scroll-ordered (not strictly newest-first) and cannot fan out across modalities the way `POST /v1/retrieve` can with wildcard namespaces ([[13-decisions/0031-retrieve-wildcard-namespace]]).

The canonical agent-tools surface ([[13-decisions/0032-agent-tools-canonical-surface]]) makes `musubi_recent` cross-modal by default. That requires a backend recency endpoint that fans out across `<tenant>/*/episodic`. This slice adds it.

## Specs to implement

- [[07-interfaces/agent-tools]] (the consumer; defines the contract `musubi_recent` must satisfy)
- [[07-interfaces/canonical-api]] (spec-update trailer: extend §Retrieve modes table to include `recent`)
- [[13-decisions/0031-retrieve-wildcard-namespace]] (no change; relied on for cross-modal fanout)
- [[13-decisions/0032-agent-tools-canonical-surface]] (the decision motivating this slice)

## Owned paths

New:

- `src/musubi/retrieve/recent.py`
- `tests/retrieve/test_recent.py`

Modified:

- `src/musubi/retrieve/orchestration.py` — extend `RetrievalQuery.mode` Literal, add `since`, dispatch branch in `_run_single`
- `src/musubi/api/routers/retrieve.py` — extend `RetrieveQuery` (router-level), validator for query_text-optional iff mode=recent
- `src/musubi/sdk/async_client.py`, `src/musubi/sdk/client.py` — expose mode + since
- `openapi.yaml` — extend `RetrieveQuery` schema
- `docs/Musubi/07-interfaces/canonical-api.md` — §Retrieve modes table
- `tests/retrieve/test_orchestration.py` — mode=recent dispatch
- `tests/api/test_retrieve_router.py` — end-to-end mode=recent
- `tests/sdk/test_async_client.py` — SDK call shape

Out of scope (separate PR):

- `src/musubi/adapters/mcp/tools.py:193` — MCP `musubi_recent` stub → real call. Folded into a follow-up PR so this one reviews cleanly as a backend addition and the MCP wiring is its own change.

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
