---
title: "ADR 0029: Plane-aligned endpoint paths for v1.0"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-23
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr, api, breaking]
updated: 2026-04-23
up: "[[13-decisions/index]]"
reviewed: false
supersedes: ""
superseded-by: ""
---

# ADR 0029: Plane-aligned endpoint paths for v1.0

**Status:** accepted
**Date:** 2026-04-23
**Deciders:** Eric
**Closes:** #232

## Context

The v0.x endpoint names for plane writes/reads are inconsistent with the plane vocabulary the rest of the API uses:

| Endpoint | Plane term | Actual path (v0.x) |
|---|---|---|
| Episodic memory | `episodic` | `/v1/memories`, `/v1/memories/batch` |
| Curated knowledge | `curated` | `/v1/curated-knowledge` |
| Concepts | `concept` | `/v1/concepts` |
| Artifacts | `artifact` | `/v1/artifacts` |

Two of the four don't match their plane vocabulary. The cross-plane retrieve request explicitly lists `planes: ["curated", "episodic", ...]` using plane names; the write endpoints should too.

This inconsistency was surfaced by the openclaw-musubi cross-validation review (Kimi 2.6, finding #31), where the plugin author's `endpointForPlane()` map conceptually used `/v1/curated/${id}` and `/v1/episodic/${id}` — matching the plane vocabulary — and the review caught that these were wrong because the actual paths are `/v1/curated-knowledge` and `/v1/memories`. The plugin's conceptual paths were closer to the intended design than the server's actual paths.

## Decision

Before the v1.0 cut, rename:

| Old | New (v1.0) |
|---|---|
| `/v1/memories` | `/v1/episodic` |
| `/v1/memories/batch` | `/v1/episodic/batch` |
| `/v1/memories/{id}` | `/v1/episodic/{id}` |
| `/v1/curated-knowledge` | `/v1/curated` |
| `/v1/curated-knowledge/{id}` | `/v1/curated/{id}` |
| `/v1/concepts` | unchanged |
| `/v1/artifacts` | unchanged |

Ship **only the new paths**. No `/v1/memories` alias, no deprecation window, no 308 redirect.

## Rationale

**Coherence.** The plane vocabulary (`episodic`, `curated`, `concept`, `artifact`) is authoritative — it's the vocabulary in the retrieve `planes` array, the Qdrant payload `plane` field, the type names, the lifecycle state machine, the docs. The write endpoints were historical accidents from an earlier data-model iteration where "episodic memory" was shortened to "memory" and "curated knowledge" was kept verbose to distinguish it from the old "memory" term.

**One-shot breakage.** v0.x → v1.0 is the one time we can make breaking surface changes without a deprecation window. Carrying an alias through v1.x locks us into doubled routing, doubled OpenAPI entries, and doubled test coverage forever — and the current set of consumers (the SDK, openclaw-musubi, openclaw-livekit) all have to re-pin on v1.0 anyway.

**Alignment with ongoing consumer work.** The openclaw-musubi plugin is planning the migration from its current `/v1/memories` + `/v1/curated-knowledge` call sites to plane-aligned paths. Shipping the rename now lets the plugin land its fix against new paths directly rather than fixing twice (first to the old paths to clear #31, then to the new paths at v1.0).

## Consequences

**Breaking change for v0.x consumers.** Any client hard-coded to `/v1/memories` or `/v1/curated-knowledge` breaks immediately after the rename. The known consumers are:
- musubi-sdk-py (updated in this PR)
- openclaw-musubi (updating in parallel, driven by plugin team)
- openclaw-livekit (same)
- operator scripts / curl examples (docs updated in this PR)

**OpenAPI regen required.** Every downstream type-generation step (SDK, plugin's openapi-typescript) picks up the new paths on next build. No manual type authoring.

**Rate-limit bucket map updated.** `src/musubi/api/app.py`'s `_PATH_TO_BUCKET` rewritten to use the new prefixes.

**Docs + tests updated in the same PR.** Every reference to the old paths in `src/`, `tests/`, `docs/Musubi/` flipped to the new paths. Historical references (ADRs, completed cross-slice tickets, migration notes describing the v0.x state) are preserved if the context is genuinely historical; otherwise updated.

## Considered alternatives

**1. Ship both paths with the old as a deprecated alias.**

Rejected. Doubles the OpenAPI surface, doubles route registration, doubles the test matrix, and locks us in through v1.x since deprecation-removal is itself a breaking change. The maintenance tax isn't worth the transitional convenience, especially when the consumer set is small and actively migrating.

**2. Rename everything (`/v1/concepts` → `/v1/concept`, etc.) for singular consistency.**

Rejected. `/v1/concepts` and `/v1/artifacts` are already plural and already match their plane names (`concept`, `artifact` — the plural form is the collection-endpoint convention). Changing them to singular would be renaming for renaming's sake.

**3. Skip the rename; document the inconsistency.**

Rejected. The v1.0 window is the only one where we can cleanly fix this. Documenting "yes, the endpoint is called `/v1/memories` but the plane is called `episodic`, sorry" permanently would be a paper cut on every new integrator's first day.

## References

- #231 (title top-level) — another pre-v1.0 breaking alignment.
- #232 — the issue this ADR closes.
- [[07-interfaces/canonical-api]] — endpoint listing updated in the same PR.
- openclaw-musubi review finding #31.
