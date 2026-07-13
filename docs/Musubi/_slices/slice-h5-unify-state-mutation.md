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
depends-on: []
blocks: ["[[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]]"]
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

## Relationship

- **Blocks:** [[_slices/slice-c6b-lifecycle-qdrant-sqlite-atomicity]] closure — C6b cannot claim
  atomicity until every state mutation routes through the coordinator.
- **Depends on:** the coordinator API existing (C6b source). So H5 and C6b source are co-dependent: C6b
  defines the coordinator; H5 makes it the *only* door. Sequencing to be settled at C6b source
  authorization.

## Status

**`ready`** (2026-07-13) — spec stub only; discovered by C6b's inventory. Owner: unassigned. Tracking
**Issue #439**. Design + contract are future work; this slice exists so the dependency is concrete and
C6b's guard red has a named destination.
