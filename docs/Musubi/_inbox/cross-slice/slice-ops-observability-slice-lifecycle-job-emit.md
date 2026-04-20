---
title: "Lifecycle workers should emit job start/end metrics"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-ops-observability
target_slice: slice-lifecycle-maturation
status: resolved
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-20
---

# Lifecycle workers should emit job start/end metrics

## Source slice

`slice-ops-observability` (PR #104).

## Problem

`docs/Musubi/09-operations/observability.md` § Test contract
bullet 7 (`test_lifecycle_job_start_end_emitted_to_events_table`)
asserts the lifecycle workers (maturation, synthesis, promotion,
reflection) emit a job-start / job-end pair to the lifecycle event
ledger AND increment `musubi_lifecycle_job_duration_seconds` +
`musubi_lifecycle_job_errors_total` on each run.

This slice cannot land that change because:

1. `src/musubi/lifecycle/` is in `forbidden_paths` for
   `slice-ops-observability` (lifecycle is owned by the
   slice-lifecycle-* family, all already shipped).
2. The lifecycle event ledger surface (`LifecycleEvent`) is owned
   by `slice-types`, also shipped — but the *emit calls* belong
   inside the worker bodies.

## Requested change

For each lifecycle worker (maturation, synthesis, promotion,
reflection), wrap the per-tick body in:

```python
from musubi.observability import default_registry

_REG = default_registry()
_DURATION = _REG.histogram(
    "musubi_lifecycle_job_duration_seconds",
    "lifecycle worker tick duration",
    labelnames=("job",),
)
_ERRORS = _REG.counter(
    "musubi_lifecycle_job_errors_total",
    "lifecycle worker tick errors",
    labelnames=("job",),
)

start = time.monotonic()
try:
    await tick()
    LifecycleEvent.write(kind="job-start-end", job=<name>, ...)
except Exception:
    _ERRORS.labels(job=<name>).inc()
    raise
finally:
    _DURATION.labels(job=<name>).observe(time.monotonic() - start)
```

The dashboard (`deploy/grafana/dashboards/musubi-lifecycle.json`) +
the email alert `lifecycle_job_failing` already query these metric
names — they just won't show data until the workers emit.

## Acceptance

- All four lifecycle workers wrap their tick body in the metric
  + ledger pair shown above.
- The Grafana lifecycle dashboard's "Job runtimes" + "Job error rate"
  panels render real data on the staging compose stack.
- `slice-ops-observability` test bullet 7 unskipped + asserts on
  the metric increment.

## Resolution

Resolved by PR: lifecycle maturation, synthesis, promotion, and reflection worker ticks now observe `musubi_lifecycle_job_duration_seconds` and increment `musubi_lifecycle_job_errors_total` on raised errors.
