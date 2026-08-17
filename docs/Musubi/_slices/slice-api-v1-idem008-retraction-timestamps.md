---
title: "Slice: API v1 IDEM-008 retraction mutation timestamps"
slice_id: slice-api-v1-idem008-retraction-timestamps
issue: 729
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, idempotency, data-integrity, api]
updated: 2026-08-17
reviewed: false
depends-on: [slice-idem007-escrow-retraction-saga]
blocks: []
---

# Slice: API v1 IDEM-008 retraction mutation timestamps

Stamp an escrow-backed episodic retraction as a real mutation: one server-owned
request timestamp must advance `updated_at` and `updated_epoch` in the same
observed-version CAS that advances the version and installs the tombstone.

## Decision boundary

- This is a behavioral correction to the existing v1 endpoint, not a wire-schema
  change.
- One `utc_now()` value supplies both temporal fields; `updated_epoch` is derived
  from that exact value.
- The timestamp is part of the same evidence-gated CAS as the tombstone. It is
  never written before escrow or by a second mutation.
- Exact receipt replay and committed-evidence adoption return the already
  committed row without restamping it.
- Escrow failure, stale version, malformed storage, and conflicting evidence
  preserve the episodic timestamps byte-for-byte.

## Owned paths

- `src/musubi/api/retraction_saga.py`
- `tests/api/test_idem008_retraction_timestamps.py`
- `docs/Musubi/_slices/slice-api-v1-idem008-retraction-timestamps.md`
- `docs/Musubi/_inbox/locks/slice-api-v1-idem008-retraction-timestamps.lock`

## Forbidden paths

- `src/musubi/types/`
- `src/musubi/planes/`
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/store/`
- `openapi.yaml`
- `proto/`

## Test contract

1. `test_retraction_advances_updated_at_and_matching_updated_epoch_in_the_same_commit`
   covers both legacy and v2 storage layouts and proves the immutable content
   generation and vectors remain unchanged.
2. `test_exact_replay_returns_the_committed_timestamp_without_restamping`
   repeats the same idempotency identity and requires byte-stable temporal fields.
3. `test_committed_evidence_adoption_returns_the_committed_timestamp_without_restamping`
   removes the completed receipt after commit and proves evidence adoption does
   not create a second mutation.
4. `test_stale_version_preserves_original_timestamps_after_verified_escrow`
   proves the escrow-first 409 path leaves the episodic row temporal fields and
   version invariant.

## Work log

- 2026-08-17 — Claimed #729 from production evidence: Aoi's escrow retraction
  advanced version 1 to 2 while retaining the original `updated_at`. Scope is a
  one-timestamp correction inside the existing saga; operator-token widening,
  wire changes, and store-primitive changes are explicitly excluded.
