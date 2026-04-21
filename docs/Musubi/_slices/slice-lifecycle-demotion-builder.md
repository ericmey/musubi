---
title: "Slice: Wire real demotion jobs into the lifecycle runner"
slice_id: slice-lifecycle-demotion-builder
section: _slices
type: slice
status: ready
owner: unassigned
phase: "6 Lifecycle"
tags: [section/slices, status/ready, type/slice, lifecycle, demotion, runner]
updated: 2026-04-21
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-promotion]]"]
blocks: []
---

# Slice: Wire real demotion jobs into the lifecycle runner

> Replace the placeholder-lambdas at job names `demotion_episodic`
> and `demotion_concept` with real `build_demotion_jobs()` helper
> output. Demotion is the smallest of the four unbuilt sweeps —
> no LLM needed; all it wants is a live QdrantClient, the two planes,
> and a `ThoughtEmitter` adapter.

**Phase:** 6 Lifecycle · **Status:** `ready` · **Owner:** `unassigned`

## Why this slice exists

Demotion keeps the retrieval surface crisp by moving unreinforced,
low-importance matured rows to `demoted` state (still indexed for
lineage, filtered out of default retrieval). Today it doesn't run.
The underlying coroutines (`demotion_episodic`, `demotion_concept`,
`demotion_artifact`) in `src/musubi/lifecycle/demotion.py` are
production-ready; what's missing is the `Job` wrapping +
`build_demotion_jobs()` helper the lifecycle runner can consume.

## Specs to implement

- [[06-ingestion/lifecycle-engine]] §Job registry
- [[04-data-model/lifecycle]] §Demotion rules
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]]

## Owned paths (you MAY write here)

- `tests/lifecycle/test_demotion.py` — add coverage for the builder.

### Coordination notes (paths NOT claimed — shared with sibling slices)

- The `ThoughtEmitter` adapter is shared with promotion + reflection
  builders; first to land creates it.
- `slice-lifecycle-promotion` technically owns `lifecycle/demotion.py`
  (demotion was bundled in that slice); this slice contributes an
  additive `build_demotion_jobs()` helper via `spec-update:`.
- The lifecycle runner needs a signature extension; coordinate with
  whichever sibling slice ships first.

## Definition of Done

![[00-index/definition-of-done]]

Plus:

- [ ] `python -m musubi.lifecycle.runner` logs
      `lifecycle-job-dispatch name=demotion_concept at=...05:00`
      on the next weekday.
- [ ] A concept with no reinforcement for 30+ days shows up in
      `state=demoted` after one weekly sweep.
- [ ] The ops-thought "Concept X demoted; reinforcement tapered off"
      is visible on a `/v1/thoughts/recv` poll from the ops channel.

## Work log

_(empty — awaiting claim)_

## PR links

_(empty)_
