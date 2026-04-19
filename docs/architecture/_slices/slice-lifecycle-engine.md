---
title: "Slice: Lifecycle scheduler"
slice_id: slice-lifecycle-engine
section: _slices
type: slice
status: in-progress
owner: cowork-auto
phase: "6 Lifecycle"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-types]]"]
blocks: ["[[_slices/slice-lifecycle-maturation]]", "[[_slices/slice-lifecycle-synthesis]]", "[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-lifecycle-reflection]]"]
---

# Slice: Lifecycle scheduler

> APScheduler-based worker. Emits LifecycleEvents. Idempotent per-job. Separate process from the API.

**Phase:** 6 Lifecycle · **Status:** `in-progress` · **Owner:** `cowork-auto`

## Specs to implement

- [[06-ingestion/lifecycle-engine]]
- [[04-data-model/lifecycle]]

## Owned paths (you MAY write here)

  - `musubi/lifecycle/__init__.py`
  - `musubi/lifecycle/transitions.py`
  - `musubi/lifecycle/events.py`
  - `musubi/lifecycle/scheduler.py`
  - `tests/lifecycle/__init__.py`
  - `tests/lifecycle/test_lifecycle.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/api/`

## Depends on

  - [[_slices/slice-types]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-lifecycle-maturation]]
  - [[_slices/slice-lifecycle-synthesis]]
  - [[_slices/slice-lifecycle-promotion]]
  - [[_slices/slice-lifecycle-reflection]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — cowork-auto — claim + test contract

- Atomic claim via `gh issue edit 11 --add-assignee @me --add-label status:in-progress`.
- Slice frontmatter flipped `ready → in-progress`; owner set to `cowork-auto`.
- Branch `slice/slice-lifecycle-engine` created; draft PR opened.
- Test contract file committed first (`tests/lifecycle/test_lifecycle.py`) per
  AGENTS.md §Test-first; reconciled slice `## Owned paths` to match the spec's
  module names — `transitions.py`, `events.py`, `scheduler.py` replace the
  placeholder `engine.py` / `states.py` / `test_engine.py`. The specs
  ([[04-data-model/lifecycle#Transition function]] and
  [[06-ingestion/lifecycle-engine]]) were already authoritative; this is a
  slice-file-only update, not a spec-prose change. Recorded here rather than
  via `spec-update:` trailer because no spec .md was edited.

#### Test Contract Closure Rule declarations

The following bullets are declared `⊘ out-of-scope` for the per-bullet
`def test_<name>` match because they are `hypothesis:` or `integration:`
prose that `tc_coverage.py` classifies as non-test. Each is covered by an
adjacent implementation:

- `hypothesis: state-machine reachability — every declared allowed transition is reachable from some state; no state is orphaned`
  → covered by `test_hypothesis_state_machine_reachability` in
  `tests/lifecycle/test_lifecycle.py`, which asserts the property directly
  over the `_ALLOWED` table.
- `hypothesis: monotone invariants — version, updated_epoch never decrease across any sequence of legal transitions`
  → covered by `test_hypothesis_monotone_invariants`, a Hypothesis property
  test over random legal transition sequences.
- `integration: full day simulation — seed corpus, advance clock 24h, assert each scheduled job ran once`
  → out-of-scope for unit tests. Implementing it requires the full per-job
  sweep implementations (maturation, synthesis, promotion, demotion,
  reflection, reconcile) that are explicitly owned by the downstream slices
  `slice-lifecycle-maturation`, `slice-lifecycle-synthesis`,
  `slice-lifecycle-promotion`, `slice-lifecycle-reflection`. Deferred to an
  integration-harness slice once those land.
- `integration: crash recovery — kill worker mid-synthesis, restart, synthesis completes from cursor`
  → same rationale; synthesis cursor lives in
  `slice-lifecycle-synthesis`'s `owns_paths`.
- `integration: ollama-outage scenario — synthesis skips cleanly, maturation skips enrichment, alerts emit`
  → same rationale; the enrichment + alert paths are owned by downstream
  sweep slices.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- _(none yet)_
