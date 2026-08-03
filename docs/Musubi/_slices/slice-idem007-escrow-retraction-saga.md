---
title: "Slice: IDEM-007 escrow-first episodic retraction saga"
slice_id: slice-idem007-escrow-retraction-saga
issue: 646
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/done, type/slice, idempotency, artifacts, data-integrity, api]
updated: 2026-08-03
reviewed: true
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
- Terminal replay and midpoint adoption are distinct. After both namespace
  authorizations, the dependency reads an exact `CompletedResponse` through a
  store-internal receipt method; the public lookup/audit models never expose
  response bytes. Receipt absence then permits lease acquisition and handler
  execution, where committed evidence closes both the landed-tombstone-before-
  receipt window and the receipt-read-to-lease race. Neither mechanism replaces
  the other.
- Existing but unparseable retraction evidence fails typed and never falls
  through to `expected_version`; recovery requires explicit operator repair.
- A second public retraction entry point beside `patch_non_embedding_payload`
  shares its private filter, CAS, readback, and exact-token release machinery.
  It requires typed evidence and calls the same storage-binding helper as
  VAL-002 before projection divergence becomes writable. No boolean bypass or
  duplicated CAS implementation is permitted.
- `MIN_RETRACTION_PREFIX_UTF8_BYTES` is a 256-byte policy floor, not a derived
  engineering limit: it keeps verbose caller prose from reducing the evidence
  quote to near-nothing. Originals at or below that floor remain quoted in full
  on the live tombstone, so raising the floor also raises the size below which
  retraction removes none of the original text from the live row. Longer
  originals quote complete grapheme clusters until at least 256 UTF-8 bytes are
  represented. Caller-owned prose is rejected rather than truncated when it
  would starve that reserve. Original length alone never causes refusal; only a
  first complete grapheme that cannot fit the bounded envelope does.
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
- `src/musubi/store/retraction_evidence.py`
- `src/musubi/store/immutable_vectors.py`
- `src/musubi/cli/validate.py`
- `tests/api/test_idem007_retraction_saga.py`
- `tests/api/test_idempotency_dependency.py`
- `tests/api/test_idem003_durable_receipts.py`
- `tests/store/test_non_embedding_patch.py`
- `tests/store/test_retraction_evidence.py`
- `tests/store/test_retraction_non_embedding_patch.py`
- `tests/cli/test_validate.py`
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
   trusting an optional caller header. A durable header cannot enable the mode
   on an ineligible route, and omitting the header cannot disable it here.
   Caller-supplied artifact reference, digest, vector basis, or raw tombstone
   content is field-bound rejected rather than silently ignored.
3. Same key and exact canonical request replays the exact completed response;
   the same key with a different digest conflicts before a second escrow or
   episodic mutation. The replay body and duplicate raw headers are byte-exact
   from the private receipt row rather than reconstructed. The same key and
   digest under a different issuer/subject/presence is a conflict, never
   adoption.
4. Each named escrow failure code leaves the entire episodic physical layout
   byte-identical. A well-formed escrow succeeds in the same test family so the
   refusal cannot pass by rejecting every head or blob.
5. A malformed existing blob and malformed existing artifact head each fail
   closed without an unhandled exception, paired with a well-formed control for
   that store.
6. A missing or malformed episodic anchor/content observation fails closed
   without artifact mutation, paired with a canonical readable-row control.
   Existing unparseable retraction evidence is a distinct typed refusal and
   cannot fall through to the expected-version path.
   The exact would-be tombstone row is strict-validated before escrow; an
   injected canonical-validation failure leaves both planes invariant.
7. Failure after verified escrow and before episodic commit leaves one safe
   deterministic artifact; exact retry reuses its verified inode/head and lands
   exactly one tombstone commit.
8. A stale `expected_version` after escrow returns typed 409, preserves the
   concurrent winner and safe escrow, and never silently rebases.
9. Another namespace carrying the same object id is whole-layout invariant.
10. Oversized, exactly-at-limit, and multibyte originals retract successfully;
    the server-built tombstone remains at most 32,768 UTF-8 bytes, contains a
    complete-grapheme prefix meeting the 256-byte reserve (or the entire shorter
    original), and records exact omitted-byte accounting. Caller prose that
    starves the reserve is rejected byte-exact rather than truncated.
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
    projection divergence remains broken. The write seam and VAL-002 exercise
    one shared helper for pointer, generation, content digest/length, prefix,
    omitted-byte, and tombstone-containment bindings.

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
- 2026-08-03 — Plan attack fixed the final implementation boundaries before the
  red head: the retract seam is a distinct public entry point sharing private
  CAS machinery, not a boolean escape hatch; its evidence precondition and
  VAL-002 use one helper. Durable receipt replay reads exact stored bytes only
  inside the dependency, while committed evidence covers the pre-receipt and
  receipt-read-to-lease windows. Principal-bound replay, unparseable-evidence
  refusal, header-mode isolation, and a 256-byte policy floor for the useful
  prefix became explicit test obligations. The record also names its consequence:
  sub-floor originals remain fully visible inside the live tombstone.
- 2026-08-03 — The first live issue-body amendment was fed through an interactive
  terminal and its echoed result exposed two missing spans. The update was not
  treated as delivered: #646 was restored from the previously-read exact body
  through a file-backed edit, then read back in full before tests continued.
- 2026-08-03 — The first remote red stopped in mypy on references to the
  deliberately absent production symbols, so it never exercised the 32
  behavioral reds reported locally. The test head was invalidated. Typed dynamic
  lookups now let format, lint, and mypy pass while pytest still reports the same
  32 failed / 25 passed focused contract; CI can now examine the thing at risk.
- 2026-08-03 — Aoi's test-contract attack found that malformed episodic state
  was covered only in the pure keyhole, not at the route ordering boundary. A
  readable positive control plus malformed legacy identity, v2 anchor, and v2
  content-target cases now require typed 409 before any artifact head/blob touch;
  each malformed episodic layout is byte-identical after refusal.
- 2026-08-03 — Production head `2c41cca` made the approved focused contract
  green at 113 tests, including an additional injected guard proving the exact
  would-be canonical tombstone is validated before durable escrow or episodic
  CAS. The first full `make check` reached 2,602 passing tests and failed only
  the two runtime-versus-snapshot OpenAPI guards on the new route; the snapshot
  and canonical API/data-model text were then regenerated and reconciled with
  the accepted ADR's body-derived namespace and escrow-before-version-fence
  ordering.
- 2026-08-03 — The documentation reconciliation exposed that the request model
  still used Pydantic's default unknown-field ignore behavior. Four field-bound
  route regressions now require caller-supplied artifact reference, original
  digest, vector basis, and raw tombstone content to fail before storage; the
  strict focused total is 117 and OpenAPI declares `additionalProperties: false`.
- 2026-08-03 — Test Contract closure for the broad episodic-memory spec keeps
  these pre-existing bullets explicitly out of this endpoint-saga lane:
  `test_maturation_sets_matured_after_ttl_and_scores_importance` and
  `test_maturation_skips_already_matured` belong to the completed
  `slice-lifecycle-maturation`; `test_query_hybrid_returns_scored_results_in_descending_order`
  belongs to completed `slice-retrieval-hybrid`;
  `test_forward_compat_reads_schema_version_0_point` remains owned by a future
  schema-migration slice; `test_perf_create_under_100ms_p95_on_reference_host`
  and `test_perf_dedup_query_under_30ms_p95` belong to the integration/perf
  harness; `hypothesis: idempotency — re-ingesting same content N times produces 1 memory with reinforcement_count == N`
  and `hypothesis: lifecycle monotonicity — state transitions never go backwards (except explicit revive operation)`
  belong to `slice-plane-episodic-followup`. IDEM-007 changes none of those
  lifecycle, retrieval, migration, property, or performance contracts.
- 2026-08-03 — Aoi approved executable/schema head `4d91c84` after independently
  reproducing 2,608 passing tests and both previously failing OpenAPI guards;
  `tc-coverage` closed at 42 passing, 3 named skips, 8 named out-of-scope, and
  zero missing. Post-main head `0293f39` carried byte-identical source, tests,
  OpenAPI, and ADR body through v1.22.0 metadata, with the version carrier as a
  differing control and the mirror digest independently resolved by both seats.
  Required remote contexts were terminal green before PR #658 squash-merged as
  `a72ba70`. Post-merge content hashes on `origin/main` equal the reviewed head;
  the pre-merge control lacks the saga module and has a different OpenAPI hash.
