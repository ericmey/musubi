---
title: "Slice: exhaustive retrieve error-code classification"
slice_id: slice-ret014-exhaustive-error-classification
issue: 619
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-progress, type/slice, retrieval, errors]
updated: 2026-08-02
reviewed: false
depends-on: [slice-issue578-truthful-hybrid-channels]
blocks: []
---

# Slice: exhaustive retrieve error-code classification

Make orchestration classification exhaustive over the closed retrieve-layer error
domain. Adding, removing, or renaming an emitted code must fail a focused test at
the producing commit rather than silently becoming an HTTP 500.

## Specs to implement

- [[05-retrieval/orchestration]]

## Decision boundary

- Mechanically inventory literal `code=` values emitted by retrieve error models.
- Preserve every current code-to-kind result; this slice changes no public error
  semantics.
- Replace the implicit `internal` fallback with an exact registry and a named set
  of intentionally internal codes.
- Reject unknown codes diagnostically. The input domain is closed over
  `src/musubi/retrieve/`, and the tests preserve that premise.
- Keep warning and error taxonomies distinct. In particular,
  `sparse_embedding_failed` intentionally exists in both.
- Do not change HTTP models, router behavior, warning vocabulary, or producers.

## Owned paths

- `src/musubi/retrieve/orchestration.py` (error classification only)
- `tests/retrieve/test_ret014_error_classification.py`
- `docs/Musubi/05-retrieval/orchestration.md` (RET-014 contract only)
- `docs/Musubi/_slices/slice-ret014-exhaustive-error-classification.md`
- `docs/Musubi/_inbox/locks/slice-ret014-exhaustive-error-classification.lock`

## Test Contract

- `test_every_literal_retrieve_error_code_has_an_explicit_classification`
- `test_existing_error_code_classifications_preserve_their_semantics`
- `test_unknown_retrieve_error_code_is_rejected_instead_of_implicitly_internal`
- `test_intentional_internal_error_codes_are_named_and_complete`
- `test_error_code_collector_rejects_new_unrecognised_code_callee`
- `test_error_code_collector_accounts_for_dynamic_forwarding_sites`
- `test_error_code_collector_walks_both_conditional_expression_arms`
- `test_retrieval_error_construction_remains_closed_over_retrieve_package`
- `test_sparse_embedding_failed_remains_distinct_in_error_and_warning_taxonomies`

## Work log

- 2026-08-02 — `codex-gpt5` reproduced the 15-code producer set from an
  immutable `origin/main` archive after rejecting a stale result from the dirty
  primary checkout. Aoi independently reproduced the same set, proved the input
  domain is closed over `retrieve/`, and required self-guards for rejected
  callees plus dynamic forwarding sites before approving implementation.
