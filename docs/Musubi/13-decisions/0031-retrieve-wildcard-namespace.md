---
title: "ADR 0031: Wildcard namespace segments for tenant-wide retrieve"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-24
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr, api, retrieve, namespace]
updated: 2026-04-24
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0031: Wildcard namespace segments for tenant-wide retrieve

**Status:** accepted
**Date:** 2026-04-24
**Deciders:** Eric

## Context

Under [[13-decisions/0030-agent-as-tenant|ADR 0030]], every channel an agent
speaks on (voice, openclaw, discord, …) writes its episodic captures into a
distinct 3-segment namespace: `nyla/voice/episodic`, `nyla/openclaw/episodic`,
and so on. Channel provenance is preserved on every row — desirable, because
"on our last call" is meaningfully different from "in the Openclaw thread".

[[13-decisions/0028-retrieve-2seg-namespace-crossplane|ADR 0028]] added the
2-seg shape (`tenant/presence`) for cross-plane retrieve **within one
channel**. There is no shape today for cross-channel retrieve **within one
tenant** — but that is the platform's foundational read pattern: *one agent,
many surfaces, one memory*. An openclaw-driven retrieve currently sees only
openclaw memories; the agent has no path to her own voice history.

The two known mitigations both hurt:
- **Plugin-side fanout** (each adapter enumerates the agent's channels and
  N-calls retrieve). Pushes household awareness into every adapter; brittle.
- **Forcing all writes to a single channel-less namespace** (`nyla/_shared/episodic`).
  Loses provenance; "where did this come from" becomes a payload field
  conventions debate, not a structural fact.

## Decision

`POST /v1/retrieve` accepts `*` as a single-segment wildcard in any segment
of the `namespace` field. Wildcards expand server-side against the live
Qdrant payload to a concrete set of (namespace, plane) targets, then run the
existing fanout pipeline ([[13-decisions/0028-retrieve-2seg-namespace-crossplane|ADR 0028]]).

**Writes reject any namespace containing `*`.** A capture must always know
which channel it came from; wildcards exist solely to read across the rows
those channel-specific writes produced.

`**` (multi-segment glob) is **not** introduced. Segment count discipline
holds: a 3-seg pattern matches only 3-seg stored namespaces, a 2-seg pattern
matches only 2-seg, and so on.

## Read shapes after this ADR

| Pattern              | Meaning                                                          |
|----------------------|------------------------------------------------------------------|
| `nyla/voice/episodic`| Single channel, single plane (unchanged from v1.0)               |
| `nyla/voice`         | Single channel, fans across `planes` (ADR 0028, unchanged)       |
| `nyla/*/episodic`    | All of Nyla's episodic across her channels — **new, primary case**|
| `nyla/*/curated`     | All of Nyla's curated across her channels                        |
| `*/voice/episodic`   | Every agent's voice episodic (cross-tenant)                      |
| `nyla/*/*`           | All of Nyla's everything (cross-channel × cross-plane)           |
| `*/*/episodic`       | The whole episodic plane (operator scope)                        |

A pattern with `*` in the trailing (plane) position is valid only when
combined with a `planes` list — same rule as the 2-seg shape, where the
plane is determined by `planes` rather than the namespace.

## Expansion semantics

1. **Enumeration source.** Qdrant payload. The router scrolls the relevant
   plane collection(s), pulling the `namespace` field only, dedups, and
   pattern-matches. No new payload fields, no registry, no precomputed index.
2. **No cache (v1).** Every retrieve runs the enumeration. At current scale
   (low thousands of rows) this is 5–20 ms; at 100k rows it would be 50–200 ms,
   at which point a TTL cache becomes worth it. Revisit when retrieve p99
   exceeds 50 ms or row count exceeds 10k, whichever first.
3. **Empty expansion = empty result.** A pattern matching no rows returns
   `{"results": [], "mode": ..., "limit": ...}` — not 404. The pattern is
   syntactically valid; no data yet is a normal state for a brand-new agent.
4. **Scope check stays strict** (ADR 0028). Every concrete target the pattern
   expanded to must be readable by the token. First denial 403s the whole
   request. Wildcard tokens (`nyla/*:r`, `*/*:r`) are the natural pairing
   for wildcard reads — but the check happens against the resolved target
   list, not the pattern.

## Internals

- Router (`src/musubi/api/routers/retrieve.py`):
  - `_resolve_targets` stays pure (string-shape validation only). It now
    accepts `*` in any segment as a syntactic validity check.
  - New helper `_expand_wildcard_targets(client, targets) -> list[(str, str)]`
    runs after `_resolve_targets`. For each target whose namespace contains
    `*`, it scrolls the matching plane collection, dedups namespace strings,
    and filters by segment-wise pattern match. Targets without wildcards
    pass through untouched.
  - Scope loop runs over the **expanded** list.
  - Orchestration receives the expanded `namespace_targets`. No orchestration
    change required — fanout already iterates concrete targets per ADR 0028.
- Writes (`src/musubi/api/routers/writes_*.py`, `src/musubi/planes/`):
  - Existing namespace-format regex (`^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$`)
    already rejects `*`. Confirm no path bypasses that regex; add a positive
    test in this slice to lock the behaviour against future drift.

## Consequences

- **Versioning.** Additive change — minor bump (v1.0 → v1.1). OpenAPI gains
  a sentence in the `namespace` schema description; no field shape change.
- **openclaw-musubi plugin.** Updates the retrieve callsites from
  `${presence}/episodic` to `${tenant}/*/episodic` (where `tenant` is the
  first segment of the resolved presence). Per-channel writes unchanged.
  This is the platform's "Nyla remembers across her surfaces" wiring.
- **openclaw-livekit.** No change required — its retrieve calls already
  use 3-seg explicit namespaces and aren't trying to span channels. Voice
  recall stays voice-tagged. Future enhancement could add a tenant-wide
  recall tool, but that's downstream.
- **Vault.** [[03-system-design/namespaces]] gains a wildcards subsection.
- **Lifecycle.** Promotion / synthesis / maturation read concrete 3-seg
  namespaces — wildcards only apply to the public retrieve endpoint, not
  to internal sweeps. Sweeps stay channel-aware.

## Trade-offs considered

**Add `tenant`/`presence`/`plane` payload fields, filter directly on them.**
Cleaner long-term — fast equality filters, no scroll. But requires a
write-side schema change and either backfill or a major bump. Deferred until
the scroll cost actually bites.

**1-seg `nyla` shape** ("everything Nyla"). Shorter, but loses the ability
to scope reads to a single plane across channels. Wildcards subsume the
1-seg case (`nyla/*/*`) and offer the per-plane variant for free.

**Plugin-side fanout (no core change).** Rejected — see Context. Every
adapter would re-implement household awareness, and the platform's job is
to *be* the household-aware substrate, not to outsource that to clients.

**Cache the expansion (60 s TTL).** Faster hot path, but adds a "did the
cache invalidate?" axis to debugging. Deferred until retrieve latency or
row count crosses the threshold above. The cache is a one-file change when
we want it.

## Deferred

- A `tenant`-indexed payload field (the option-C path). Trigger: scroll
  cost crossing 50 ms p99, or row count crossing 10k. New ADR at that point.
- Wildcard support in `POST /v1/retrieve/stream` — same pattern, same
  expansion. Out of scope for this slice; trivial follow-up once the router
  helper exists.
- Wildcard support in `GET /v1/thoughts/stream` `?namespace=` — different
  shape (subscription, not retrieval); revisit if a use case appears.
