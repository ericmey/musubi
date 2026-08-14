---
title: "Slice: Bound HTTP metric endpoint labels"
slice_id: slice-obs-http-route-labels-709
issue: 709
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/done, type/slice, observability]
updated: 2026-08-14
reviewed: true
depends-on: []
blocks: []
---

# Slice: Bound HTTP metric endpoint labels

Production metrics used the literal request path as the `endpoint` label. An
object-addressed GET therefore minted a new counter and histogram family for
every object ID. On 2026-08-14, `musubi-core` occupied 111,791 of Mimir's
150,000 active-series allowance and blocked collector remote write.

## Decision boundary

- Use Starlette's matched route template after routing has completed.
- Collapse every unmatched path into one `<unmatched>` sentinel.
- Apply the same bounded label to request, duration, and 5xx metrics.
- Do not raise Mimir's limit as a substitute for fixing the source.

## Owned paths

- `src/musubi/observability/metrics_middleware.py`
- `tests/observability/test_observability.py`
- `docs/Musubi/_slices/slice-obs-http-route-labels-709.md`

## Test Contract

- Multiple literal object IDs produce one route-template label.
- Raw object IDs do not appear in rendered metrics.
- Arbitrary 404 paths produce one bounded sentinel label.
- Exception-path counters and 5xx metrics use the same route template.

## Work log

- 2026-08-14 — Bound the endpoint label to the router's authority rather than
  the caller-supplied URL path. The production collector remains rolled back
  until the old high-cardinality series age out and remote write recovers.
