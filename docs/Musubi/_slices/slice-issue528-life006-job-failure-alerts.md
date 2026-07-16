---
owner: gemini-3-1-shiori
status: done
issue: 528
title: "Slice: LIFE-006 durable lifecycle job failure alerts"
slice_id: slice-issue528-life006-job-failure-alerts
section: _slices
type: slice
phase: "Lifecycle"
tags:
  - section/slices
  - status/done
  - type/slice
updated: 2026-07-15
reviewed: true
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
- `tests/lifecycle/test_life006_alerts.py`
- `docs/Musubi/_slices/slice-issue528-life006-job-failure-alerts.md`

## Forbidden paths
- Qdrant logic, LLM adapter implementations, new subsystems.

## Test Contract
- `test_job_success_emits_no_alert`
- `test_job_failure_emits_exactly_one_durable_alert`
- `test_alert_emission_failure_remains_visible_and_does_not_crash_runner`
- `test_alert_emission_timeout_is_bounded_and_does_not_crash_runner`

## Definition of Done
- Durable alert mechanism implemented for job failures.
- Production `_main_async` wires `thought_emitter` into `LifecycleRunner`.
- Alert body includes UTC timestamp + `trace_id` (no `str(exc)` leakage).
- `make check` is fully passing.

## Work log
- 2026-07-15 `cursor-grok`: addressed Copilot review — wire production emitter, enrich alert with timestamp/trace_id, fix slice frontmatter/Test Contract/owns_paths, keep OTel mocks on monkeypatch.
