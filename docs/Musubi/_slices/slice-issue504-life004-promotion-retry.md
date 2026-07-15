---
owner: gemini-3-1-shiori
status: in-review
issue: 504
title: "Slice: LIFE-004 promotion retry classification"
slice_id: slice-issue504-life004-promotion-retry
section: _slices
type: slice
phase: "Lifecycle"
tags:
  - section/slices
  - status/in-review
  - type/slice
updated: 2026-07-15
reviewed: false
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
- `tests/lifecycle/test_promotion.py`

## Forbidden paths
- Qdrant logic, LLM adapter implementations.

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
- Integration and property bullets 30-33 are pre-existing downstream test requirements explicitly out-of-scope for the LIFE-004 fix boundary.
- Updated `promotion.py` to differentiate ValueError (deterministic) from generic Exception (transient) during LLM rendering.
- Updated post-render pipeline to catch PromotionPolicyError as deterministic, while broad exceptions (OSError, RuntimeError, TypeError, unclassified infra issues) remain explicitly transient.
- Converted single failing test into four distinct tests covering transient vs deterministic cases for both stages.
