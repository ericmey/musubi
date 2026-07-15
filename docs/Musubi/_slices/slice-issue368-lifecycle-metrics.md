---
owner: gemini-3-1-shiori
status: in-progress
issue: 368
title: "Slice: METRICS-001 lifecycle job metrics visibility"
slice_id: slice-issue368-lifecycle-metrics
section: _slices
type: slice
phase: "Observability"
tags:
  - section/slices
  - status/in-progress
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
- `src/musubi/observability/registry.py`
- `tests/lifecycle/test_runner.py`
- `tests/observability/test_registry.py`

## Forbidden paths
- C4/DQ branches. Core lifecycle logic outside of metrics instrumentation.

## Test Contract
- Job registry and instrumentation equality.
- Correct label increments on success/failure.
- Duration metric tracking per job.

## Definition of Done
- Centralized wrapper implemented.
- Redundant metrics removed.
- Tests pass.
- Alerts updated if necessary.

## Work log
