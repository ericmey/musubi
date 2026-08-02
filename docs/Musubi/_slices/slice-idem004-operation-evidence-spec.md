---
title: "Slice: IDEM-004 operation evidence specification"
slice_id: slice-idem004-operation-evidence-spec
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, api, security, idempotency]
updated: 2026-08-02
reviewed: false
issue: 605
depends-on: [slice-api-v1-idempotency-receipts]
blocks: []
---

# Slice: IDEM-004 operation evidence specification

Issue #603 records three production outbox rows that cannot be reconciled by a
completed-response receipt alone. This documentation-only slice defines the durable
operation-evidence contract, the honest legacy-resolution vocabulary, and the
implementation sequence before any server or client runtime change.

## Scope

- Add ADR 0040 for pre-mutation reservation, typed rejection, mutation-coupled
  evidence, exact orphan reconciliation, and legacy non-retroactivity.
- Preserve the current public API until object-side evidence makes new states
  actionable.
- Define the two-value legacy-resolution schema and its fail-closed gates.
- Record the current read-only-auditor limitation without weakening receipt
  authorization in this slice.
- Decompose runtime work into legacy resolver, internal journal, mutation evidence,
  client adoption, and deployment proof.

## Specs to implement

- [[13-decisions/0039-durable-client-idempotency-receipts]]
- [[13-decisions/0040-durable-operation-evidence-and-legacy-resolution]]
- [[07-interfaces/canonical-api]]

## Owned paths

- `docs/Musubi/13-decisions/0040-durable-operation-evidence-and-legacy-resolution.md`
- `docs/Musubi/_slices/slice-idem004-operation-evidence-spec.md`
- `docs/Musubi/07-interfaces/canonical-api.md`

## Forbidden paths

- `src/**`
- `tests/**`
- `openapi.yaml`
- `proto/**`
- `deploy/**`

## Test Contract

1. `test_adr_defines_exactly_two_legacy_resolution_kinds`
2. `test_operator_abandon_requires_unknown_delivery_state`
3. `test_receipt_observation_requires_principal_scope_and_timestamp`
4. `test_boundary_evidence_names_content_bytes_and_one_sided_probe`
5. `test_legacy_operation_evidence_is_explicitly_non_retroactive`
6. `test_internal_journal_slice_exposes_no_public_lookup_states`
7. `test_public_client_adoption_is_blocked_until_mutation_evidence`
8. `test_later_mutation_cannot_erase_unresolved_operation_evidence`
9. `test_read_only_auditor_limitation_remains_explicitly_open`

These are documentation assertions in this slice. Each runtime bullet must be
transcribed into a passing test, explicit deferral, or named out-of-scope work-log
entry in the implementation slice that owns the relevant code.

## Work log

- 2026-08-02 — Eric authorized Aoi and Yua to treat Musubi safety work as a
  mandatory two-seat job, with Yua driving and Aoi independently verifying every
  destructive or delivery-bearing conclusion.
- 2026-08-02 — Exact immutable outbox inspection found three
  `post_attempted=1` rows: 55,327, 680, and 36,367 UTF-8 content bytes. Aoi
  independently reproduced the first-executable-statement 32,768-byte guard across
  every deployable repository tag and confirmed that only the 680-byte row remains
  historically ambiguous.
- 2026-08-02 — Live v1.18.2 proof used 32,769 ASCII content bytes. Exact durable
  receipt lookup was `absent` before and after, namespace identity count remained
  1,511, and the request returned HTTP 500. Exactly 32,768 was not exercised. The
  verified control object was soft-archived by exact object id.
- 2026-08-02 — All three stored legacy request digests recomputed byte-for-byte
  from their persisted request bodies, persisted content types, and current
  canonical digest function. One-bit body and forced-`text/plain` negative controls
  both changed the digest. The legacy resolver can therefore require the exact
  body-and-content-type digest without a compatibility shim.
- 2026-08-02 — Draft PR #604 opened from a clean `origin/main` worktree before
  documentation edits. Parent defect #603 remains open; this spec slice is Issue
  #605 and must not falsely close the production defect.
