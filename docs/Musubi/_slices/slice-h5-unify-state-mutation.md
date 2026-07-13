---
title: "Slice: H5 — unify all lifecycle state mutation behind LifecycleTransitionCoordinator"
slice_id: slice-h5-unify-state-mutation
section: _slices
type: slice
status: ready
owner: unassigned
phase: "Lifecycle-audit 2026-07-13 — H5 mutation-path unification (C6b dependency)"
tags: [section/slices, status/ready, type/slice, lifecycle, atomicity, refactor]
updated: 2026-07-13
reviewed: false
depends-on: ["[[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]]"]
blocks: []
issue: 439
---

# Slice: H5 — unify all lifecycle state mutation behind LifecycleTransitionCoordinator

Route **every** production lifecycle state mutation through the single `LifecycleTransitionCoordinator`
boundary that C6b introduces. Surfaced by C6b's structural inventory (Yua correction G): C6b makes
Qdrant↔SQLite transitions atomic only for callers that go through the coordinator, but state is mutated
directly by many bypassing paths today.

## The bypass inventory (verified 2026-07-13)

Direct `state` mutation via `set_payload` or a plane's own `transition()` — every one bypasses any
coordinator and can produce mutation-without-audit:

- **5 plane `transition()` methods** — `planes/{episodic,concept,thoughts,artifact,curated}/plane.py`
  (each `set_payload`s `state`+`version` and emits its own `LifecycleEvent`), called by
  `lifecycle/promotion.py`, `lifecycle/demotion.py` (×5), `api/routers/writes_concept.py` (×2).
- **Direct lifecycle `set_payload` of `state`** — `lifecycle/maturation.py:893`,
  `lifecycle/synthesis.py:718`, `lifecycle/demotion.py:380`, and canonical `lifecycle/transitions.py:252`.

## Scope

Migrate all of the above to `LifecycleTransitionCoordinator`. The mechanical guard shipped RED in C6b
([[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] guard G1: AST/rg forbidding direct
`state`-writing `set_payload` outside the coordinator) flips green when this slice lands.

## Relationship (acyclic — no circular dependency)

- **Depends on:** [[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] **Phase 1** — H5 consumes the
  `LifecycleTransitionCoordinator` API that C6b Phase 1 defines + implements.
- **Gates C6b closure (not a frontmatter `blocks` edge — that would be circular):** C6b Phase 1 lands
  with C6b still OPEN; H5 then migrates every mutation path; C6b's guard G1 (and G2/G3 for the migrated
  callers) go green; only THEN can C6b close as a defect. This "closure gate" is a documented state, not a
  DAG edge, per Yua's no-circular-dependency ruling (2026-07-13).

## Status

**`ready`** (2026-07-13) — spec stub only; discovered by C6b's inventory. Owner: unassigned. Tracking
**Issue #439**. Design + contract are future work; this slice exists so the dependency is concrete and
C6b's guard red has a named destination.
