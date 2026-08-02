---
title: "Slice: IDEM-006 read-only two-seat receipt audit"
slice_id: slice-idem006-readonly-receipt-audit
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/done, type/slice, api, idempotency, security]
updated: 2026-08-02
reviewed: true
issue: 607
depends-on: [slice-idem004-operation-evidence-spec]
blocks: []
---

# Slice: IDEM-006 read-only two-seat receipt audit

Allow a read-only operator second seat to verify one exact authorization-bound
durable receipt without weakening the ordinary write-authorized recovery lookup or
creating a guessable-key existence oracle.

## Contract

- Require both operator scope and namespace read authority before storage access.
- Require the complete target principal and receipt identity; expose no list,
  prefix, or enumeration form.
- Never echo the raw idempotency key.
- Return server-observed auditor identity, scopes, and timestamp plus requested
  namespace, operation, digest, and an opaque target identity hash.
- Return `found` or `absent` to a cross-principal auditor. Collapse a wrong-digest
  conflict to `absent`; preserve conflict only for the owning principal.
- Audit durable receipt state only; do not disclose process-local lease state.
- Document the household operator trust boundary explicitly.

## Owned paths

- `src/musubi/api/idempotency_receipts.py`
- `src/musubi/api/routers/idempotency_receipts.py`
- `tests/api/test_idem006_receipt_audit.py`
- `docs/Musubi/07-interfaces/canonical-api.md`
- `docs/Musubi/13-decisions/0040-durable-operation-evidence-and-legacy-resolution.md`
- `docs/Musubi/_slices/slice-idem006-readonly-receipt-audit.md`

## Test contract

1. Missing auth, operator-only, and read-only callers cannot reach storage.
2. Operator plus namespace-read authority can confirm an exact cross-principal
   receipt and receives server observer provenance.
3. Cross-principal wrong digest returns `absent`, not `conflict`.
4. The owning principal retains conflict fidelity.
5. Responses never echo the raw idempotency key and expose no enumeration route.
6. The existing write-authorized receipt lookup contract remains unchanged.

## Work log

- 2026-08-02 — Aoi's read-only operator token could not inspect the ordinary
  receipt lookup because that route correctly requires namespace write authority.
- 2026-08-02 — Aoi found the first audit design leaked guessed-key existence via
  cross-principal conflict. The accepted contract collapses that state to absent.
- 2026-08-02 — Aoi independently reviewed rebased head `493b030` and accepted
  the authorization order, disclosure boundary, conflict collapse, and narrowed
  `absent` contract with no remaining findings. Marked terminal before merge to
  satisfy the repository slice-hygiene gate for PR #613.
