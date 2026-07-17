---
title: "Slice: IDEM-003 durable idempotency receipt lookup"
slice_id: slice-api-v1-idempotency-receipts
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "7-adapters"
tags: [section/slices, status/in-progress, type/slice, api, security, idempotency]
updated: 2026-07-17
reviewed: false
issue: 593
depends-on: [slice-idempotency-phase-b]
blocks: []
---

# Slice: IDEM-003 durable idempotency receipt lookup

An external verified-delivery client can lose a successful capture response after
Musubi has accepted the object. Re-POSTing after the ordinary replay cache expires
can create or reinforce a second mutation. This slice adds a durable,
authorization-bound completed-response receipt and an additive lookup endpoint so
the client can recover the accepted object without guessing.

## Scope

- Persist clean terminal 2xx receipts before an idempotent success response is
  released to the client.
- Bind receipts to issuer, subject, presence, method, operation, authorized
  namespace, idempotency key, and byte-exact request digest.
- Return `found`, `absent`, `conflict`, or `in_flight` without disclosing another
  principal's or namespace's receipt.
- Preserve the ordinary POST replay TTL. Receipt retention is independent.
- Keep `WEB_CONCURRENCY=1`; multi-worker leases and orphaned server-operation
  reconciliation remain Issue #558.

## Owned paths

- `src/musubi/api/idempotency.py`
- `src/musubi/api/idempotency_observer.py`
- `src/musubi/api/idempotency_receipts.py`
- `src/musubi/api/app.py`
- `src/musubi/api/routers/idempotency_receipts.py`
- `src/musubi/settings.py`
- `tests/api/test_idem003_durable_receipts.py`
- `tests/test_config.py`
- `docs/Musubi/07-interfaces/canonical-api.md`
- `docs/Musubi/13-decisions/0039-durable-client-idempotency-receipts.md`
- `docs/Musubi/_slices/slice-api-v1-idempotency-receipts.md`
- `docs/Musubi/_inbox/locks/api-v1-idempotency-receipts.lock`
- `openapi.yaml`

## Forbidden paths

- `src/musubi/planes/**`
- `src/musubi/retrieve/**`
- `src/musubi/lifecycle/**`
- `src/musubi/adapters/**`
- `proto/**`
- deployment worker-count changes

## Test Contract

1. `test_receipt_survives_replay_cache_expiry_and_process_recreation`
2. `test_receipt_lookup_requires_authentication_before_storage_access`
3. `test_receipt_lookup_rejects_cross_namespace_access_without_disclosure`
4. `test_receipt_lookup_binds_operation_key_and_request_digest`
5. `test_success_response_is_not_released_before_durable_receipt_commit`
6. `test_receipt_store_failure_returns_failure_not_unreceipted_success`
7. `test_absent_and_in_flight_are_distinct_from_found`
8. `test_exact_object_id_namespace_and_response_hash_round_trip`

## Work log

- 2026-07-17 — Eric authorized the Musubi-side receipt capability after the
  fleet-tools crash matrix proved that client-side idempotency alone cannot safely
  recover a lost POST response after cache expiry. Split from Issue #558 so this
  additive client-recovery API does not falsely claim multi-worker server safety.
