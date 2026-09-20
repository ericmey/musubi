---
title: "Slice: API v1 RET-012 retraction lifecycle quarantine"
slice_id: slice-api-v1-ret012-retraction-quarantine
issue: 731
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/done, type/slice, retraction, lifecycle, data-integrity, api]
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
2. `test_evidence_adoption_repairs_pre_quarantine_state_without_rewriting_content`
   proves a valid evidence-bearing pre-quarantine row is repaired during receipt-loss
   adoption without rewriting vectors, immutable content, or committed timestamps;
   `test_evidence_adoption_releases_committed_done_token_without_reapplying_retraction`
   proves the same path finishes exact post-commit token release without reapplying the mutation.
3. `test_retracted_provisional_row_cannot_reenter_maturation_after_one_hour`
   advances lifecycle time and proves zero selection, zero enrichment, archived
   state, and importance 1.
4. `test_exact_private_receipt_replay_is_byte_identical_and_does_not_run_saga_twice`,
   `test_committed_evidence_adoption_returns_the_committed_timestamp_without_restamping`,
   `test_stale_version_preserves_original_timestamps_after_verified_escrow`, and
   `test_existing_unparseable_evidence_fails_typed_without_falling_through_to_version`
   keep the existing IDEM-007/008 replay, adoption, and failure contracts green.

## Definition of Done

- Every Test Contract function passes on both supported episodic layouts where parametrized.
- Receipt-loss adoption repairs quarantine state and releases an attributable committed token
  without rewriting immutable content, vectors, timestamps, or version.
- The focused IDEM-007/008 and RET-012 suite and full `make check` pass.
- Independent review certifies the exact merge tree before merge.

## Work log

- 2026-08-17 — Claimed #731 after production exact read proved Aoi's retracted
  provisional false row matured after one hour to importance 6. This slice uses
  the existing archived state and changes only the dedicated retraction saga.
