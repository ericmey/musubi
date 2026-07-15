---
owner: gemini-3-1-shiori
status: in-review
issue: 368
title: "Slice: METRICS-001 lifecycle job metrics visibility"
slice_id: slice-issue368-lifecycle-metrics
section: _slices
type: slice
phase: "Observability"
tags:
  - section/slices
  - status/in-review
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: METRICS-001 lifecycle job metrics visibility

## Context
Fix lifecycle job metrics and failure visibility (Issue #368). Implement centralized scheduler-dispatch wrapper so every registered job emits duration + errors with exact canonical dispatch name, removing double-counting.

## Specs to implement
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]]
- Issue #368

## Owned paths
- `src/musubi/lifecycle/runner.py`
- `src/musubi/lifecycle/maturation.py`
- `src/musubi/lifecycle/promotion.py`
- `src/musubi/lifecycle/reflection.py`
- `src/musubi/lifecycle/synthesis.py`
- `tests/lifecycle/test_maturation.py`
- `tests/lifecycle/test_promotion.py`
- `tests/lifecycle/test_reflection.py`
- `tests/lifecycle/test_runner_metrics.py`
- `tests/lifecycle/test_synthesis.py`

## Forbidden paths
- C4/DQ branches. Core lifecycle logic outside of metrics instrumentation.

## Test Contract
- `test_runner_dispatch_observes_job_duration_on_success`
- `test_runner_dispatch_observes_job_duration_and_errors_on_crash`
- `test_all_default_jobs_are_instrumented`

## Definition of Done
- Centralized wrapper implemented.
- Redundant metrics removed.
- Tests pass.
- Alerts updated if necessary.

## Work log
- 2026-07-15 — Centralized lifecycle duration and error metrics in
  `LifecycleRunner._dispatch`, keyed by the exact registered `Job.name`.
  Removed per-sweep wrappers so each dispatched job records one duration and a
  crashing job records one error. Added success, crash, and complete default-job
  registry coverage; removed obsolete tests that asserted direct sweep calls
  emitted scheduler metrics. Integrated current `origin/main` once before
  handoff and ran the lifecycle-focused suite plus repository gates.
