---
owner: gemini-3-1-shiori
status: in-progress
issue: 528
title: "Slice: LIFE-006 durable lifecycle job failure alerts"
slice_id: slice-issue528-life006-job-failure-alerts
section: _slices
type: slice
phase: "Lifecycle"
tags:
  - section/slices
  - status/in-progress
  - type/slice
updated: 2026-07-15
reviewed: false
depends-on: []
blocks: []
---
# Slice: LIFE-006 durable lifecycle job failure alerts

## Context
Lifecycle job failures currently only log and increment an in-memory counter; the existing skipped contract promises a durable ops-alerts Thought (Issue #528). Implement a safe sync/async integration seam to emit one bounded non-secret durable alert per failed execution using the existing Thought plane.

## Specs to implement
- Issue #528

## Owned paths
- `src/musubi/lifecycle/runner.py`
- `tests/lifecycle/test_runner.py`

## Forbidden paths
- Qdrant logic, LLM adapter implementations, new subsystems.

## Test Contract
- Pending definition...

## Definition of Done
- Durable alert mechanism implemented for job failures.
- `make check` is fully passing.

## Work log
