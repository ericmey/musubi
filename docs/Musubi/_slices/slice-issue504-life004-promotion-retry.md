---
owner: gemini-3-1-shiori
status: in-progress
issue: 504
title: "Slice: LIFE-004 promotion retry classification"
slice_id: slice-issue504-life004-promotion-retry
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
# Slice: LIFE-004 promotion retry classification

## Context
Fix `promotion_attempts` logic (Issue #504). Currently, transient infrastructure failures burn a strike, leading to concepts being incorrectly blocked from promotion. We must classify deterministic/policy failures vs transient ones so only deterministic errors increment the attempts count.

## Specs to implement
- Issue #504

## Owned paths
- `src/musubi/lifecycle/promotion.py`
- `tests/lifecycle/test_promotion.py`

## Forbidden paths
- Qdrant logic, LLM adapter implementations.

## Test Contract
- Deterministic failure increments attempts.
- Transient failure leaves attempts unchanged.

## Definition of Done
- Error classification logic is in place.
- `make check` is fully passing.

## Work log
