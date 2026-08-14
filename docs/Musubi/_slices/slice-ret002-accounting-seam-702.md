---
title: "Slice: RET-002 accounting-failure test seam"
slice_id: slice-ret002-accounting-seam-702
issue: 702
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-review, type/slice, test-integrity]
updated: 2026-08-14
reviewed: false
depends-on: [slice-ret002-access-accounting]
blocks: []
---

# Slice: RET-002 accounting-failure test seam

Release PR #700 exposed that the accounting-failure contract test could return
a legitimate retrieval-timeout 503 before reaching the monkeypatched
`account_delivered` exception whose bounded 500 behavior it claimed to test.

## Decision boundary

- Do not change production retrieval timeouts or error classification.
- Stub both orchestration calls to deterministic empty success envelopes in
  this one seam test.
- Keep `account_delivered` as the only injected failure and prove its raw
  exception detail is redacted.
- Assert that both recent and ranked retrieval calls completed before the
  accounting exception is observed.

## Owned paths

- `tests/api/test_ret002_context_accounting.py`
- `docs/Musubi/_slices/slice-ret002-accounting-seam-702.md`

## Specs to implement

- [[_slices/slice-ret002-accounting-seam-702]] — this slice and its `## Test
  Contract` below.

## Test Contract

- `test_context_accounting_failure_returns_internal_not_raw`

## Work log

- 2026-08-14 — Rebound the test to the seam it names. The focused case passed
  50 consecutive local runs; Ruff and strict mypy passed. Production code is
  unchanged.
