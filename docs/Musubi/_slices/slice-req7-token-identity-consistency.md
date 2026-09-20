---
title: "Slice: REQ-7 token identity consistency"
slice_id: slice-req7-token-identity-consistency
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/done, type/slice, api, security, auth]
updated: 2026-09-20
reviewed: false
issue: 412
depends-on: []
blocks: []
---

# Slice: REQ-7 token identity consistency

Close the deferred REQ-7 contract from
[[13-decisions/ADR-auth-boundary-consolidation]]: a validated token identity
cannot claim a presence in one tenant while carrying concrete namespace scopes
for another tenant. The configured issuer remains signature-bound, the subject
remains required, and the presence is made structurally usable as a
`tenant/presence` identity.

## Scope

- Validate the presence claim as exactly two non-empty, non-wildcard segments.
- Require the subject to equal the presence identity with `/` encoded as `-`,
  matching the authority's canonical principal-id format.
- Reject every concrete namespace scope whose tenant differs from the presence
  tenant, even when another scope matches.
- Preserve same-tenant shared scopes and the existing operator/global scope
  forms.
- Keep `jti` outside the stable identity tuple.

## Specs to implement

- [[_slices/slice-req7-token-identity-consistency]] — this slice's executable
  contract is the `## Test Contract` below.

The governing design record is D6 / REQ-7 in
`docs/Musubi/13-decisions/ADR-auth-boundary-consolidation.md`; it records the
decision but does not carry a separate Test Contract.

## Owned paths

- `src/musubi/auth/tokens.py`
- `tests/api/test_req7_token_identity_invariant.py`
- `tests/api/conftest.py` — mint internally consistent synthetic identities by
  default while permitting explicit inconsistency regressions.
- `docs/Musubi/13-decisions/ADR-auth-boundary-consolidation.md`
- `docs/Musubi/_slices/slice-req7-token-identity-consistency.md`
- `docs/Musubi/_inbox/locks/slice-req7-token-identity-consistency.lock`

## Also changed, owned elsewhere

These are bounded synthetic-token fixture corrections required by the stricter
validator; the behavior each historical test proves is unchanged.

- `tests/api/test_req8_public_invalid_protected_bearer.py` — keep the
  authorization control token REQ-7-valid while remaining out of scope.
- `tests/api/test_retrieve_stream.py` — bind existing `nyla/*` fixtures to a
  `nyla` presence.
- `tests/api/test_thoughts_check_history.py` — keep the authorization control
  same-tenant but out of scope.
- `tests/api/test_idem007_retraction_saga.py` — keep authorization-order and
  different-principal fixtures internally valid under REQ-7.
- `tests/api/test_idem006_receipt_audit.py` — normalize observer and target
  identities to the canonical subject encoding.
- `tests/integration/conftest.py` and `tests/integration/test_harness.py` — make
  the live harness token satisfy REQ-7 and lock that identity relationship.

## Forbidden paths

- `src/musubi/api/**` — the canonical API surface is unchanged.
- `src/musubi/auth/scopes.py` — authorization matching remains unchanged; this
  slice validates the token identity before an `AuthContext` is constructed.
- Deployment topology and durable idempotency ownership tracked by Issue #558.

## Test Contract

1. `test_inconsistent_presence_vs_scope_must_be_rejected`
2. `test_cross_tenant_scope_among_matching_scopes_must_be_rejected`
3. `test_presence_must_be_a_two_segment_identity`
4. `test_subject_must_match_declared_presence`
5. `test_consistent_presence_and_scope_is_accepted`
6. `test_same_tenant_shared_scope_is_accepted`
7. `test_operator_and_global_scopes_remain_valid`
8. `test_wrong_issuer_is_rejected`
9. `test_missing_presence_is_rejected`
10. `test_d6_identity_is_issuer_subject_presence_not_jti`

## Work log

### 2026-09-20 14:17 - codex-gpt5 - claim

- Claimed via the `pick-slice` workflow after the operator explicitly reopened
  deferred Issue #412. Draft PR #799; isolated branch
  `slice/req7-token-identity-consistency`.

### 2026-09-20 15:02 - codex-gpt5 - implementation complete

- Enforced the D6 identity tuple at token validation: presence must be a
  concrete `tenant/presence`, subject must use the authority's canonical
  slash-to-hyphen encoding, and every concrete scope must stay in the declared
  tenant. Operator, global, and same-tenant shared scopes remain valid; `jti`
  remains outside stable identity.
- Updated synthetic token helpers and narrowly corrected historical fixtures
  so their intended authorization and idempotency assertions execute after the
  stricter validation boundary.
- Resolved the Copilot subject/presence review thread with a regression test and
  exact-head implementation evidence. Follow-up review also tightened concrete
  presence validation against embedded wildcards, corrected the live integration
  token producer, and made the D6 `jti` exclusion test use distinct token IDs.
- Test Contract closure: all ten bullets pass in
  `tests/api/test_req7_token_identity_invariant.py` (lines 42, 60, 79, 91, 101,
  114, 123, 132, 153, and 173); no skips or out-of-scope declarations.
- Verification: `make check` passed with 2,840 passed, 195 skipped, 146
  deselected, one expected xfail, and 88.91% total coverage;
  `make tc-coverage SLICE=slice-req7-token-identity-consistency` passed 10/10;
  `make agent-check` passed with warnings only.
- PR #799 is ready for independent review. The vault status is `done` before
  merge as required by the PR-closing-slice gate; Issue #412 remains
  `status:in-review` until GitHub closes it on merge.
