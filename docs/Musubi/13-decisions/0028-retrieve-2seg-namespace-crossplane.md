---
title: "ADR 0028: 2-segment namespace for cross-plane retrieve, strict scope fanout"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-23
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr, api, retrieve]
updated: 2026-04-23
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0028: 2-segment namespace for cross-plane retrieve, strict scope fanout

**Status:** accepted
**Date:** 2026-04-23
**Deciders:** Eric
**Closes:** #209

## Context

`POST /v1/retrieve` up through v0.4.0 required a 3-segment
`tenant/presence/plane` namespace AND took a `planes` list. The
stored-row filter is literal, so a request like
`{namespace: "foo/bar/episodic", planes: ["curated", "episodic"]}`
only returned rows with `payload.namespace == "foo/bar/episodic"` —
invisible to curated/concept even when the caller named them.

Net: the `planes` field was only useful when the namespace's stored
plane segment matched every entry in the list, which is structurally
impossible because namespaces are per-plane by design.

Every cross-plane consumer (openclaw-musubi's `recall` + supplement
modules; openclaw-livekit's voice tools) worked around this with
client-side fanout: one retrieve call per plane, merge + dedup
client-side. Wire tax: ~3x for a typical recall.

## Decision

`POST /v1/retrieve` accepts **either** namespace shape:

- **3-segment** (`tenant/presence/plane`): single-plane query.
  Current behaviour preserved bit-for-bit. `planes`, if supplied,
  must be a single-element list matching the namespace's trailing
  plane — otherwise the server 400s (callers were almost certainly
  silently discarding their intent).
- **2-segment** (`tenant/presence`): cross-plane query. Each entry
  in `planes` is expanded to `<namespace>/<plane>` server-side. The
  orchestrator runs the pipeline per target concurrently and merges
  results by score. One HTTP call, N internal pipeline runs.

Scope check is **strict**: every expanded (namespace, plane) target
must be readable by the token. The first denial 403s the entire
request. Rationale: a token asking for a plane it can't read is
almost always a misconfiguration; silently omitting that plane
would mask the bug. The plugin side can still implement permissive
"best effort" fanout on top of this if it wants to — it knows the
token's scope map; the server only sees the ask.

## Expansion rules

| Body                                                   | Targets                                                     |
|--------------------------------------------------------|-------------------------------------------------------------|
| `ns="a/b/episodic"`                                    | `[(a/b/episodic, episodic)]`                                |
| `ns="a/b/episodic"`, `planes=["episodic"]`             | `[(a/b/episodic, episodic)]` (tautology, allowed)           |
| `ns="a/b/episodic"`, `planes=["curated"]`              | **400** — namespace pins plane, list contradicts            |
| `ns="a/b"`                                             | `[(a/b/episodic, episodic)]` (default single-plane)         |
| `ns="a/b"`, `planes=["episodic", "curated"]`           | `[(a/b/episodic, episodic), (a/b/curated, curated)]`        |
| `ns="a/b"`, `planes=["misspelled"]`                    | **400** — unknown plane                                     |
| `ns="a/b/c/d"`                                         | **400** — namespace must be 2- or 3-segment                 |

## Internals

- Router (`src/musubi/api/routers/retrieve.py`): `_resolve_targets`
  produces the `(namespace, plane)` list, the scope loop strictly
  checks each, and `namespace_targets` rides on the orchestration
  dict alongside the original `namespace` and `planes`.
- Orchestrator (`src/musubi/retrieve/orchestration.py`): when
  `namespace_targets` has a single entry, `_run_single` handles the
  call with behaviour identical to pre-#209. When there are multiple
  targets, `asyncio.gather(return_exceptions=True)` fans the pipeline
  out per target; `Ok` results are merged with `object_id` dedup and
  sorted by score. A transient failure on one plane degrades to no
  hits from that plane (not a whole-request failure); an `internal`
  error from any plane surfaces as 5xx because the merged response
  would silently under-report.

## Consequences

- Cross-plane consumers can collapse their fanout back to a single
  HTTP call. openclaw-musubi and openclaw-livekit follow this ADR
  in companion PRs.
- `RetrieveQuery`'s wire shape is unchanged — same fields, same
  defaults. The dispatch is driven by the namespace's segment count,
  not by a new field. That keeps the OpenAPI snapshot stable.
- Response shape is unchanged. Every row still carries its stored
  `namespace`, so consumers that want to route by plane continue to.
- **Strict scope is a breaking semantic** for any client that was
  relying on over-scoped requests silently succeeding with a subset.
  No such client exists today — both known consumers do client-side
  fanout with per-plane scope already — so the behaviour change is
  forward-only.

## Alternatives considered

**Explicit namespace-per-plane map** (`namespaces: {plane: ns}`).
More verbose, removes ambiguity, but wire shape is bigger and
callers have to build the map themselves. The 2-segment shorthand
covers the common case with less ceremony.

**Keep single-plane-per-call, document the fanout as client-side.**
Shipped behaviour before this ADR. Rejected because every consumer
reimplements the same fanout-dedup-sort pattern, and we don't want
v1.0 to ship with an API that requires boilerplate for its most
common use case.

**Permissive scope fanout** (skip planes the token can't read).
Rejected per the strictness argument above. Silent partial results
are the wrong default for a security-adjacent check. Plugins can
opt into permissive behaviour by filtering the `planes` list they
send, which is visible in the request shape.

## Deferred

- If we later add `mode="deep"` semantics that need a *global*
  cross-plane rerank (vs. per-plane rerank + score sort), this ADR
  does not cover that. The current behaviour is per-plane rerank
  inside each target, then a naive score sort at the merge step.
  Good enough for today; revisit when eval harness says otherwise.
