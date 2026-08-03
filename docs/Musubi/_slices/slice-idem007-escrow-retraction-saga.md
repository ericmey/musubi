---
title: "Slice: IDEM-007 escrow-first episodic retraction saga"
slice_id: slice-idem007-escrow-retraction-saga
issue: 646
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, idempotency, artifacts, data-integrity, api]
updated: 2026-08-03
reviewed: false
depends-on: [slice-art004-exact-byte-escrow, slice-data002-typed-retraction-evidence]
blocks: []
---

# Slice: IDEM-007 escrow-first episodic retraction saga

Implement ADR 0042's single public saga: authorize both storage planes, escrow
the exact current episodic bytes, build bounded typed evidence server-side, and
commit one non-reembedding tombstone mutation. No public half-operation ships.

## Decision boundary

- `POST /v1/episodic/{object_id}/retract` requires an `Idempotency-Key`, durable
  completed-response semantics, and an exact `expected_version`.
- The request carries only caller-owned truth fields. Namespace derivation,
  original bytes/digest/length, artifact address, tombstone structure, vector
  basis, operation marker, and request digest are server-owned.
- Episodic and derived sibling-artifact write authorization both finish before
  any row, blob, head, idempotency lease, or hash is observed.
- Escrow is the first durable boundary. Every escrow error, including malformed
  existing blob or head state, produces zero episodic mutation.
- The episodic commit is one-shot, layout-aware, namespace-bound, and
  non-reembedding. V2 changes only the anchor; legacy changes only its identity
  row. A stale winner is never rebased.
- Same identity and canonical request digest may adopt an exact completed
  retraction. A different identity or digest cannot overwrite evidence, orphan
  its escrow, or form a retraction chain.
- Option B / evidence discard remains unauthorized on every error path.
- Fleet-tools consumer cutover remains a separate final lane under #611.

## Specs to implement

- [[13-decisions/0042-escrow-backed-episodic-retraction]]
- [[07-interfaces/canonical-api]]
- [[04-data-model/episodic-memory]]

## Owned paths

- `src/musubi/api/retraction_saga.py`
- `src/musubi/api/routers/writes_episodic.py`
- `src/musubi/api/idempotency_dependency.py`
- `src/musubi/api/idempotency_receipts.py`
- `tests/api/test_idem007_retraction_saga.py`
- `tests/api/test_idempotency_dependency.py`
- `tests/api/test_idem003_durable_receipts.py`
- `openapi.yaml`
- `docs/Musubi/07-interfaces/canonical-api.md`
- `docs/Musubi/04-data-model/episodic-memory.md`
- `docs/Musubi/13-decisions/0042-escrow-backed-episodic-retraction.md`
- `docs/Musubi/_slices/slice-idem006-readonly-receipt-audit.md`
- `docs/Musubi/_slices/slice-idem007-escrow-retraction-saga.md`
- `docs/Musubi/_inbox/locks/slice-idem007-escrow-retraction-saga.lock`

## Test contract

1. Both write authorizations complete before idempotency acquisition or any
   episodic, blob, or artifact-head observation; an unauthorized sibling
   artifact namespace reaches neither plane.
2. Missing, duplicate, or conflicting `Idempotency-Key` input fails typed before
   mutation; this endpoint always installs durable receipt mode rather than
   trusting an optional caller header.
3. Same key and exact canonical request replays the exact completed response;
   the same key with a different digest conflicts before a second escrow or
   episodic mutation.
4. Each named escrow failure code leaves the entire episodic physical layout
   byte-identical. A well-formed escrow succeeds in the same test family so the
   refusal cannot pass by rejecting every head or blob.
5. A malformed existing blob and malformed existing artifact head each fail
   closed without an unhandled exception, paired with a well-formed control for
   that store.
6. A missing or malformed episodic anchor/content observation fails closed
   without artifact mutation, paired with a canonical readable-row control.
7. Failure after verified escrow and before episodic commit leaves one safe
   deterministic artifact; exact retry reuses its verified inode/head and lands
   exactly one tombstone commit.
8. A stale `expected_version` after escrow returns typed 409, preserves the
   concurrent winner and safe escrow, and never silently rebases.
9. Another namespace carrying the same object id is whole-layout invariant.
10. Oversized, exactly-at-limit, and multibyte originals retract successfully;
    the server-built tombstone remains at most 32,768 UTF-8 bytes, contains a
    non-empty complete-grapheme prefix, and records exact omitted-byte accounting.
11. If one complete grapheme cannot fit after bounded server structure and
    caller prose, retraction fails typed before episodic mutation.
12. V2 retraction changes only the anchor payload/version: content-point payload,
    generation, and vectors are whole-dict invariant. Legacy retraction keeps its
    vectors and commits one identity-row payload change.
13. Response returns object id, new version, exact escrow reference, and typed
    evidence. Durable receipt appears only after both phases complete and binds
    the exact response.
14. A matching landed tombstone is adopted after response loss; a distinct
    re-retraction identity or digest is a typed conflict and cannot replace the
    first evidence.
15. VAL-002 reports accepted legacy and v2 retractions clean while ordinary
    projection divergence remains broken.

## Work log

- 2026-08-03 — Claimed #646 after ART-004 and DATA-002 closed. The live issue
  moved from `status:blocked` to `status:in-progress`; ADR 0042 is accepted.
  The first contract carries two review lessons forward mechanically: every
  escrow midpoint failure asserts byte-identical episodic physical state, and
  every new blob/head/anchor read has a malformed-state case paired with a
  same-store well-formed positive control so fail-closed cannot mean reject-all.
- 2026-08-03 — The first hygiene run refused three owned paths because the
  completed IDEM-006 slice still said `status:blocked` although #607 is closed
  with `status:done` after its live audit proof. This lane repairs that stale
  vault half of the dual-update contract before taking shared receipt/API paths;
  the correction changes no IDEM-006 code or contract.
