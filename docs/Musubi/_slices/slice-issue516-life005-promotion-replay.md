---
owner: gemini-3-1-shiori
status: in-review
issue: 516
title: "Slice: LIFE-005 promotion replay identity consistency"
slice_id: slice-issue516-life005-promotion-replay
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
# Slice: LIFE-005 promotion replay identity consistency

## Context
Fix `promoted_to` identity drift during promotion replay (Issue #516). If a transient infra issue occurs after the vault write but before the concept transitions, the next sweep will replay the promotion. Since it's an idempotent rewrite to an existing `promoted_from == concept.object_id` file, we must reuse the `object_id` from the existing file's frontmatter rather than generating a new KSUID, to prevent orphaning the original Qdrant point.

## Specs to implement
- [[06-ingestion/promotion]]

## Owned paths
- `docs/Musubi/06-ingestion/promotion.md`
- `src/musubi/lifecycle/promotion.py`
- `tests/lifecycle/test_promotion.py`

## Forbidden paths
- Qdrant logic, LLM adapter implementations.

## Test Contract
- `test_idempotent_replay_reuses_existing_vault_object_id`
- `test_idempotent_replay_reuses_vault_id_when_qdrant_also_exists`
- `test_idempotent_replay_fails_closed_on_missing_vault_object_id`
- `test_idempotent_replay_fails_closed_on_invalid_vault_object_id`
- `test_idempotent_replay_adopts_persisted_qdrant_identity`

## Definition of Done
- `curated_id` is parsed from the vault if present and valid during idempotent rewrite.
- `make check` is fully passing.

## Work log
