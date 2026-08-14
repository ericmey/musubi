---
title: "Slice: deduplicate effective synthesis samples after capping"
slice_id: slice-syn-post-cap-dedupe-725
issue: 725
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "6 Lifecycle"
tags: [section/slices, status/done, type/slice, lifecycle, synthesis]
updated: 2026-08-14
reviewed: true
depends-on: [slice-lifecycle-synthesis]
blocks: []
---

# Slice: deduplicate effective synthesis samples after capping

Production acceptance for v1.25.0 produced two synthesized concepts from the
same 20 source memories. The original oversized tag groups were distinct, so
the pre-cap fingerprint check admitted both; deterministic importance-first
sampling then collapsed both groups to the same effective LLM input.

## Specs to implement

- [[06-ingestion/concept-synthesis]]

## Decision boundary

- Preserve the existing full-cluster fingerprint check.
- Fingerprint the effective member set after deterministic sampling and skip a
  repeated sample before any LLM or embedding call.
- Do not add semantic-output deduplication or change the existing-match state
  policy.
- Members excluded by the sample remain eligible in the candidates pool.

## Owned paths

- `src/musubi/lifecycle/synthesis.py`
- `tests/lifecycle/test_synthesis.py`
- `docs/Musubi/06-ingestion/concept-synthesis.md`
- `docs/Musubi/_slices/slice-syn-post-cap-dedupe-725.md`
- `docs/Musubi/_inbox/locks/slice-syn-post-cap-dedupe-725.lock`

## Test Contract

- `test_oversized_clusters_deduplicate_after_sampling`

## Work log

- 2026-08-14 — A controlled production synthesis pass formed two clusters and
  created two rows with identical title, content, and the same 20 `merged_from`
  object IDs. The regression reproduces the mechanism with two distinct
  oversized tag groups that collapse to one deterministic top-five sample.
- 2026-08-14 — The regression failed on current main with two LLM calls and two
  concepts, then passed after adding the post-cap effective-input fingerprint.
  The two unsampled members remain in the candidates pool.
- 2026-08-14 — Full gate passed: 2,686 passed, 195 skipped, 140 deselected,
  2 expected failures; formatting, lint, type checking, and 88.93% coverage
  passed. Slice Test Contract closure passed 32/32 machine-readable entries
  (25 passing tests, 3 documented skips, 4 non-test entries).
- 2026-08-14 — Independent exact-head review mutation-tested both boundaries:
  disabling the duplicate skip failed this regression, while forcing one
  constant fingerprint failed four synthesis tests. Fresh CI passed on the
  reviewed implementation head.

spec-update: docs/Musubi/06-ingestion/concept-synthesis.md
