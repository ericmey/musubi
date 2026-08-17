---
title: "Slice: API v1 RET-012 retraction lifecycle quarantine"
slice_id: slice-api-v1-ret012-retraction-quarantine
issue: 731
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, retraction, lifecycle, data-integrity, api]
updated: 2026-08-17
reviewed: false
depends-on: [slice-api-v1-idem008-retraction-timestamps]
blocks: []
---

# Slice: API v1 RET-012 retraction lifecycle quarantine

Make escrow-backed retraction terminal for ordinary memory lifecycle authority.
The evidence-gated CAS sets `state=archived` with the tombstone, so a false row
cannot later mature, regain importance, synthesize, or promote. Exact GET and
the escrow artifact remain available for correction and audit.

## Specs to implement

- [[07-interfaces/canonical-api]]

## Decision boundary

- Retraction is not deletion: the bounded tombstone, original vector, strict
  evidence, and exact-byte escrow remain recoverable and exactly readable.
- `archived` is the existing terminal state used to exclude a row from ordinary
  ranked settled recall and the provisional maturation selector.
- State, importance, timestamps, evidence, and tombstone change in one CAS.
- Exact replay and evidence adoption retain the already committed archived row.

## Owned paths

- `src/musubi/api/retraction_saga.py`
- `tests/api/test_ret012_retraction_quarantine.py`
- `docs/Musubi/_slices/slice-api-v1-ret012-retraction-quarantine.md`
- `docs/Musubi/_inbox/locks/slice-api-v1-ret012-retraction-quarantine.lock`

## Forbidden paths

- `src/musubi/types/`
- `src/musubi/planes/`
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/store/`
- `openapi.yaml`
- `proto/`

## Test Contract

1. `test_retraction_archives_legacy_and_v2_rows_from_any_active_state` proves
   provisional and matured originals become archived without vector or immutable
   generation changes.
2. `test_retracted_provisional_row_cannot_reenter_maturation_after_one_hour`
   advances lifecycle time and proves zero selection, zero enrichment, archived
   state, and importance 1.
3. Existing IDEM-007/008 replay, adoption, and failure tests remain green.

## Work log

- 2026-08-17 — Claimed #731 after production exact read proved Aoi's retracted
  provisional false row matured after one hour to importance 6. This slice uses
  the existing archived state and changes only the dedicated retraction saga.
