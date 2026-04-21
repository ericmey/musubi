---
title: "Slice: Wire real reflection jobs into the lifecycle runner"
slice_id: slice-lifecycle-reflection-builder
section: _slices
type: slice
status: ready
owner: unassigned
phase: "6 Lifecycle"
tags: [section/slices, status/ready, type/slice, lifecycle, reflection, runner]
updated: 2026-04-21
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-reflection]]", "[[_slices/slice-vault-sync]]"]
blocks: []
---

# Slice: Wire real reflection jobs into the lifecycle runner

> Replace the placeholder-lambda at job name `reflection_digest` with
> a real `build_reflection_jobs()` helper that composes
> `run_reflection_sweep` against live `VaultWriter`, `ThoughtEmitter`,
> and `ReflectionLLM` implementations. Ships the daily digest.

**Phase:** 6 Lifecycle · **Status:** `ready` · **Owner:** `unassigned`

## Why this slice exists

Reflection writes a daily digest markdown file into the vault that
summarises capture activity, promotions, demotions, contradictions,
and revisit candidates. The `run_reflection_sweep` coroutine in
`src/musubi/lifecycle/reflection.py` is production-ready; what's
missing is:

- A real `ReflectionLLM` (different Protocol from maturation +
  synthesis — takes a structured summary, returns a narrative).
- A real `VaultWriter` (shared with promotion — write to
  `vault/<ns>/reflections/<date>.md`).
- A real `ThoughtEmitter` (to ping "daily digest ready" to ops).
- The `build_reflection_jobs()` Job wrapper.

## Specs to implement

- [[06-ingestion/lifecycle-engine]] §Job registry
- [[06-ingestion/reflection]] (the sweep's spec)
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]]

## Owned paths (you MAY write here)

- `src/musubi/llm/reflection_client.py` (new — httpx `ReflectionLLM`
  impl + `src/musubi/llm/prompts/reflection/v1.txt`).
- `tests/llm/test_reflection_client.py` (new).

### Coordination notes (paths NOT claimed — shared with sibling slices)

- `slice-lifecycle-reflection` owns `lifecycle/reflection.py`; this
  slice contributes an additive `build_reflection_jobs()` helper.
- The `ThoughtEmitter` adapter + shared `VaultWriter` impl are
  shared with promotion-builder; first to land creates.
- The lifecycle runner needs a signature extension; coordinate with
  whichever sibling slice ships first.

## Dependencies on other P3 slices

- `VaultWriter` and `ThoughtEmitter` adapters are shared with
  `slice-lifecycle-promotion-builder`. Whichever lands first creates
  them; the other reuses.

## Definition of Done

![[00-index/definition-of-done]]

Plus:

- [ ] `python -m musubi.lifecycle.runner` dispatches
      `reflection_digest` at `06:00` UTC and produces a file at
      `vault/<ns>/reflections/YYYY-MM-DD.md` with the expected
      summary sections.
- [ ] A thought lands in the ops channel linking to the new digest.

## Work log

_(empty — awaiting claim)_

## PR links

_(empty)_
