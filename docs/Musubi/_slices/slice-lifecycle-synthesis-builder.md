---
title: "Slice: Wire real synthesis jobs into the lifecycle runner"
slice_id: slice-lifecycle-synthesis-builder
section: _slices
type: slice
status: in-review
owner: claude-code-opus-4-7
phase: "6 Lifecycle"
tags: [section/slices, status/in-review, type/slice, lifecycle, synthesis, runner]
updated: 2026-04-21
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-synthesis]]"]
blocks: []
---

# Slice: Wire real synthesis jobs into the lifecycle runner

> The lifecycle worker that went live on 2026-04-21 runs the documented
> cron schedule, but the `synthesis` job name still resolves to the
> placeholder-lambda from `build_default_jobs()`. This slice replaces
> that placeholder with a real `build_synthesis_jobs()` helper that
> returns `Job` objects bound to the real `synthesis_run` coroutine +
> a production `SynthesisOllamaClient`.

**Phase:** 6 Lifecycle · **Status:** `in-review` · **Owner:** `claude-code-opus-4-7`

## Why this slice exists

`src/musubi/lifecycle/runner.py::build_lifecycle_jobs()` composes the
real job registry by merging the documented default jobs with any
builder-produced jobs passed via `maturation_jobs=`. Today it accepts
maturation jobs only (via `build_maturation_jobs()`) — every other
sweep's production wiring is pending. For synthesis specifically:

- `src/musubi/lifecycle/synthesis.py::synthesis_run` is the real
  coroutine.
- `SynthesisOllamaClient` Protocol is defined there; the only satisfier
  in the codebase is `_NoOpOllamaClient` (used by the debug-trigger
  endpoint) and the stub returned by `default_ollama_client()` if one
  existed — neither is a production client.
- The `HttpxOllamaClient` in `src/musubi/llm/ollama.py` satisfies the
  **maturation** Protocol (`score_importance`, `infer_topics`), not
  the synthesis one (`synthesize_cluster`, `check_contradiction`).
  Either the two Protocols merge (likely) or we ship a second
  synthesis-specific client class.

## Specs to implement

- [[06-ingestion/lifecycle-engine]] §Job registry
- [[06-ingestion/concept-synthesis]] (the sweep's spec)
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]] §Consequences —
  "real builders for the non-maturation sweeps" is the open item.

## Owned paths (you MAY write here)

- `src/musubi/llm/synthesis_client.py` (new) OR extend
  `src/musubi/llm/ollama.py` with `synthesize_cluster` + `check_contradiction`
  methods + two new prompt files under `src/musubi/llm/prompts/synthesis/v1.txt`
  and `src/musubi/llm/prompts/contradiction/v1.txt`.
- `src/musubi/lifecycle/synthesis.py` — add a `build_synthesis_jobs()`
  helper following the exact shape of `maturation.build_maturation_jobs`
  (file-lock wrapper, `asyncio.run(sweep())` inside `_runner`, matching
  `trigger_kwargs={"hour":3,"minute":0}`, `grace_time_s=3600`).
- `tests/llm/test_synthesis_client.py` (new) — mirror of
  `tests/llm/test_ollama.py`.

### Coordination notes (paths NOT claimed — shared with sibling slices)

- The lifecycle runner needs a small extension to
  `build_lifecycle_jobs()` to accept `synthesis_jobs=` and merge the
  same way it does `maturation_jobs=`. The four builder slices all
  need this edit; the first to land sets the signature shape and
  later PRs rebase.

## Forbidden paths

- `src/musubi/lifecycle/maturation.py` (done, owned by another slice).
- `src/musubi/lifecycle/scheduler.py` (the Job primitive itself — frozen).
- `src/musubi/api/` (the debug-trigger endpoint is API-surface).

## Definition of Done

![[00-index/definition-of-done]]

Plus:

- [x] `python -m musubi.lifecycle.runner` logs `lifecycle-job-dispatch
      name=synthesis` at the next `03:00` cron window. *(Verified in
      unit tests — `test_build_lifecycle_jobs_wires_synthesis_builders`.
      Production cron-fire awaits the next deploy to musubi.example.local.)*
- [ ] Against `musubi.example.local`, a forced run produces a
      `SynthesisReport` with `concepts_created > 0` or
      `contradictions_detected > 0` when synthetic data is present.
      *(Deferred to the end-to-end deploy step after merge.)*
- [x] The lifecycle-runner integration test (exists or added) covers
      synthesis at the same fidelity as maturation. *(Added
      `test_build_lifecycle_jobs_wires_synthesis_builders` +
      `_merges_all_three_builder_groups` matching the demotion/maturation
      pattern.)*

## Work log

### 2026-04-21 — claude-code-opus-4-7

Shipped in PR #165:

- `HttpxOllamaClient.synthesize_cluster()` + `check_contradiction()`
  satisfying the `SynthesisOllamaClient` Protocol, with pydantic-validated
  JSON responses and fail-soft (`None`) on Ollama outage.
- First-party prompts at `src/musubi/llm/prompts/synthesis/v1.txt` and
  `…/contradiction/v1.txt`.
- `build_synthesis_jobs()` in `lifecycle/synthesis.py` — per-namespace
  sweep with `file_lock("synthesis.lock")` coalesce, cadence `03:00 UTC`.
- `_discover_episodic_namespaces()` helper that paginates the episodic
  scroll until Qdrant returns `offset=None` (so a new presence's
  records can't be silently dropped by the first page cutoff).
- `build_lifecycle_jobs()` grown a `synthesis_jobs=` kwarg, composed
  alongside `maturation_jobs=` and `demotion_jobs=`.

**Known deviations flagged during review:**

- Commit ordering: the work landed as a single `feat(...)` commit
  rather than a preceding `test(...)` commit. Tests are in the branch
  but the tests-first commit order was not followed. Noted for the
  next slice.
- Spec update: [[06-ingestion/concept-synthesis]] §Failure Handling
  previously documented atomic-batch writes. The parent slice shipped
  one-by-one writes; I updated the spec text to match current reality
  and flagged the optimization as deferred. Commit trailer:
  `spec-update: docs/Musubi/06-ingestion/concept-synthesis.md`.

## PR links

- PR #165 — https://github.com/ericmey/musubi/pull/165
