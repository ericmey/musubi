---
title: "Slice: IDEM-003 durable idempotency receipt lookup"
slice_id: slice-api-v1-idempotency-receipts
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "7-adapters"
tags: [section/slices, status/done, type/slice, api, security, idempotency]
updated: 2026-07-17
reviewed: true
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

- For requests that opt in with `Idempotency-Receipt: durable`, persist clean
  terminal 2xx receipts before an idempotent success response is released to the
  client.
- Bind receipts to issuer, subject, presence, method, operation, authorized
  namespace, idempotency key, and byte-exact request digest.
- Return `found`, `absent`, `conflict`, or `in_flight` without disclosing another
  principal's or namespace's receipt.
- Preserve the ordinary POST replay TTL. Receipt retention is independent.
- Keep `WEB_CONCURRENCY=1`; multi-worker leases and orphaned server-operation
  reconciliation remain Issue #558.

## Specs to implement

- [[07-interfaces/canonical-api]]
- [[13-decisions/0039-durable-client-idempotency-receipts]]

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
- 2026-07-17 — Tests-first contract landed at `ea7efe4`; implementation landed at
  `cde7657`. Durable mode is explicit (`Idempotency-Receipt: durable`) so ordinary
  24h key reuse remains unchanged. Receipt identity includes authenticated
  issuer/subject/presence, operation, authorized namespace, key, and request digest.
  Receipt and replay state commit before success bytes; transport loss replays, and
  restart/expiry recovery uses the authenticated lookup.
- 2026-07-17 — Final proof on `cde7657`: focused security/idempotency/compatibility
  matrix 56 passed with the one pre-existing invalid SEC-002 probe skipped; strict
  runtime/OpenAPI parity passed; Test Contract closure passed (15 applicable, three
  longstanding canonical-API deferrals); `make check` passed with 2,435 passed,
  195 skipped, 136 deselected, and five documented xfails. Ready for independent
  security and contract review; `reviewed` remains false.
- 2026-07-17 — Review hardening superseded that first review head. `0a96b1d`
  removed batch capture from durable eligibility and moved the shared eligibility
  gate to the routed dependency edge, preventing a multi-object write followed by
  a single-object receipt failure. `47c0a80` strengthened the regression with real
  collection-count evidence that the rejected batch performs no mutation. Focused
  final matrix: 58 passed plus the separately rerun storage mutation proof. Full
  `make check` on the byte-identical production code: 2,436 passed, 195 skipped,
  136 deselected, five documented xfails. Aoi re-approved `0a96b1d`; Tama
  recertified it and requested the stronger proof now present in `47c0a80`. Final
  exact-head readback remains required before merge; `reviewed` stays false.
- 2026-07-17 — Merged PR #594 into `main` as `53d9c14` and auto-closed Issue
  #593. The final exact-head review chain at `e005d28` included Aoi APPROVE and
  Tama CERTIFY after adversarial checks of authorization-before-storage,
  identity isolation, receipt/replay/send ordering, final-status metrics, strict
  digest validation, and typed lookup-backend failures. Exact-head CI passed with
  2,441 tests, 194 skips, 136 deselections, and five documented xfails; smoke and
  vault hygiene also passed. This unblocks external verified-delivery adapters,
  including the fleet-tools drainer, to adopt inspect-before-repost recovery.
  Multi-worker leases and mutation-before-receipt orphan reconciliation remain
  explicitly owned by Issue #558.
