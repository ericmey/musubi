---
title: "Slice: REQ-7 token identity consistency"
slice_id: slice-req7-token-identity-consistency
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "8-ops"
tags: [section/slices, status/in-progress, type/slice, api, security, auth]
updated: 2026-09-20
reviewed: false
issue: 412
depends-on: [slice-idempotency-phase-b]
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
- `tests/api/test_req8_public_invalid_protected_bearer.py` — keep the
  authorization control token REQ-7-valid while remaining out of scope.
- `tests/api/test_retrieve_stream.py` — bind existing `nyla/*` fixtures to a
  `nyla` presence.
- `tests/api/test_thoughts_check_history.py` — keep the authorization control
  same-tenant but out of scope.
- `docs/Musubi/13-decisions/ADR-auth-boundary-consolidation.md`
- `docs/Musubi/_slices/slice-req7-token-identity-consistency.md`
- `docs/Musubi/_inbox/locks/slice-req7-token-identity-consistency.lock`

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
