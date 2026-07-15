---
owner: gemini-3-1-shiori
status: done
issue: 504
title: "Slice: LIFE-004 promotion retry classification"
slice_id: slice-issue504-life004-promotion-retry
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
# Slice: LIFE-004 promotion retry classification

## Context
Fix `promotion_attempts` logic (Issue #504). Currently, transient infrastructure failures burn a strike, leading to concepts being incorrectly blocked from promotion. We must classify deterministic/policy failures vs transient ones so only deterministic errors increment the attempts count.

## Specs to implement
- [[06-ingestion/promotion]]

## Owned paths
- `docs/Musubi/06-ingestion/promotion.md`
- `src/musubi/lifecycle/promotion.py`
- `src/musubi/llm/promotion_client.py`
- `tests/lifecycle/test_promotion.py`
- `tests/llm/test_promotion_client.py`

## Forbidden paths
- Qdrant logic and unrelated LLM adapter implementations.

## Test Contract
- `test_deterministic_rendering_failure_increments_attempts`
- `test_transient_rendering_failure_leaves_attempts_unchanged`
- `test_deterministic_post_render_failure_increments_attempts`
- `test_transient_post_render_failure_leaves_attempts_unchanged`
- `test_deterministic_model_validation_failure_increments_attempts`


## Definition of Done
- Error classification logic is in place.
- `make check` is fully passing.

## Work log
- Property and integration bullets 34-37 are pre-existing downstream test requirements explicitly out-of-scope for the LIFE-004 fix boundary.
- Updated the render boundary so only explicit `PromotionPolicyError` is deterministic; transport/envelope `ValueError` and other infrastructure exceptions remain transient.
- Updated `HttpxPromotionClient` to raise `PromotionPolicyError` only for validated-body policy rejection, while malformed upstream envelopes remain transient.
- Updated post-render model construction to wrap local `ValueError`/`TypeError` validation as `PromotionPolicyError`; vault, Qdrant, transition, and other infrastructure failures remain transient.
- Added five focused lifecycle discriminators plus production-client coverage for deterministic body rejection versus transient envelope failure.
