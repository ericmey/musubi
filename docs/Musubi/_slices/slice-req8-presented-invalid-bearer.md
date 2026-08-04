---
title: "Slice: REQ-8 presented-invalid bearer rejection"
slice_id: slice-req8-presented-invalid-bearer
section: _slices
type: slice
status: done
owner: aoi
phase: "8-ops"
tags: [section/slices, status/done, type/slice, api, security, auth]
updated: 2026-08-04
reviewed: true
issue: 413
depends-on: [slice-auth-boundary-red-contract]
blocks: []
---

# Slice: REQ-8 presented-invalid bearer rejection

Closes row 8 of [[_slices/slice-auth-boundary-red-contract]] — "public absent-bearer
stays public; presented-invalid fails; protected absent fails" — which that slice
named as a contract and left `TO-WRITE` for the public pair.

**This is a dedicated implementation slice, not a continuation of the red-contract
slice.** The red contract owns the *statement* of row 8 and keeps its historical
`#402`-at-head record unchanged; this slice owns the *implementation and closure* of
the public half. The protected-absent half was already satisfied by
`test_*_no_token_must_be_401` across sec002/003/004 and is untouched here.

## The defect

A public route ignored the `Authorization` header entirely. `GET /v1/ops/health` with
`Bearer not-a-real-token` returned 200, byte-identical to the anonymous response. A
client that presents a credential is asserting an identity; serving it anonymously
tells that client it is authenticated when it is not, and the failure is silent and
lands on the caller.

Absent credentials stay public. That is a different fact and remains a 200.

## Pre-code survey (behavioural, not structural)

Run against a real `create_app`, absent vs presented-invalid, before any code. A first
pass counted 19 routes with no auth *dependency* and nearly reported that as the blast
radius; it was wrong, because most authorize inside the handler. Exactly **five** routes
ignored a presented-invalid bearer:

`/v1/ops/health`, `/v1/ops/status`, `/v1/ops/metrics`, `/v1/docs`, `/v1/openapi.json`

`/v1/retrieve`, `/v1/retrieve/stream`, `/v1/context`, `/v1/namespaces`, `/v1/thoughts/*`,
`/v1/idempotency/receipts/*` and `/v1/concepts/{id}/promote` already returned 401 both
ways.

## Scope

- Reject a presented-but-invalid bearer on every route, public included, as a typed
  401 through the canonical error envelope.
- Preserve public routes for **absent** credentials — no health-probe regression.
- Validate a presented bearer exactly once per request; reuse the edge decision in the
  handler rather than re-validating the same token.
- Keep authorization uncached: scope and operator requirements still execute.
- Split the deployment smoke probes so a stale ops token cannot masquerade as a
  service-health failure.

## Specs to implement

- [[_slices/slice-req8-presented-invalid-bearer]] — this implementation slice's contract IS the
  `## Test Contract` below, following the convention of the sibling auth slices
  (`slice-auth-boundary-red-contract`, `slice-auth-boundary-phase-a`), which self-link for the
  same reason. The design record is `ADR-auth-boundary-consolidation` (REQ-8) and the contract
  statement is row 8 of the red-contract slice; neither carries its own `## Test contract`
  section, so linking them would report `no-test-contract` rather than anything about this work.

## Owned paths

Coordination claims only. `check.py` treats this section as "an in-flight slice claims
this file", so it lists just the paths this slice authors or is primary on. Everything
else it changed is declared in the next section with its real owner — declaring a file
here that a shipped slice already owns would assert a merge hazard that does not exist.

- `src/musubi/api/presented_bearer.py` (new)
- `src/musubi/auth/middleware.py`
- `tests/api/test_req8_public_invalid_protected_bearer.py`
- `deploy/smoke/lib.sh`
- `deploy/smoke/check_api.sh`
- `deploy/smoke/check_observability.sh`
- `docs/Musubi/_slices/slice-req8-presented-invalid-bearer.md` (new)

## Also changed, owned elsewhere

Declared for completeness so ownership matches the delta. These are bounded edits inside
files another slice authored; none is claimed above.

| path | owner | why this slice touched it |
|---|---|---|
| `src/musubi/api/app.py` | `slice-auth-boundary-phase-a` | register `PresentedBearerGuard` + its import |
| `tests/api/test_ret007_http_warnings.py` | `slice-ret007-degradation` | removed one fictional `Bearer fake` header |
| `tests/api/test_ret007_status_and_telemetry.py` | `slice-ret007-degradation-impl` | removed one fictional `Bearer fake` header |
| `tests/api/test_ret007_telemetry_boundary.py` | `slice-issue522-ret013-recency-context` | removed two fictional `Bearer fake` headers |
| `tests/api/test_ret009_include_lineage.py` | unclaimed by any slice | removed six fictional `Bearer fake` headers |
| `docs/Musubi/_slices/slice-auth-boundary-red-contract.md` | itself | row 8 status + reciprocal `blocks` edge |
| `docs/Musubi/13-decisions/ADR-auth-boundary-consolidation.md` | unclaimed by any slice | REQ-8 status rows |

The ten removed headers are accepted scope, not incidental: those suites monkeypatch
`musubi.api.routers.*.authenticate_request` to return `Ok`, so an invalid credential in
the header only ever "worked" because auth was mocked at the router. Reviewed and
accepted as test-honesty repair.

## Forbidden paths

- `deploy/kong/**` — staged future-state, out of the current deployment denominator.
- `tests/api/test_req7_token_identity_invariant.py` — REQ-7 (#412) stays deferred.
- Anything closing #558.

## Test Contract

1. `test_public_absent_bearer_stays_public`
2. `test_public_presented_invalid_bearer_must_fail`
3. `test_protected_absent_bearer_must_fail`
4. `test_protected_presented_invalid_bearer_must_fail`
5. `test_every_public_route_rejects_a_presented_invalid_bearer`
6. `test_every_public_route_still_serves_without_a_bearer`
7. `test_public_route_accepts_a_VALID_bearer`
8. `test_expired_bearer_is_rejected_on_a_public_route`
9. `test_empty_bearer_value_is_rejected_not_treated_as_absent`
10. `test_non_bearer_authorization_scheme_passes_through`
11. `test_rejection_uses_the_canonical_typed_error_envelope`
12. `test_valid_bearer_on_protected_route_validates_exactly_once`
13. `test_reused_context_still_enforces_the_route_requirement`
14. `test_401_hint_is_truthful_on_a_protected_route`

## Design notes

**Not reusing `_bearer_token`.** It collapses absent, non-bearer, and empty-`Bearer`
all to `None` — correct when *requiring* auth, wrong when *detecting presentation*. An
empty `Bearer ` would have read as absent and slipped through the hole this closes.

**ASGI middleware, not a dependency.** The hole is on routes that carry no dependency by
design, and it must also cover `/v1/docs` and `/v1/openapi.json`, which are
framework-generated. Mounted innermost of the response-observing middleware so a
rejection is still counted rather than bypassing telemetry.

**Single validation.** The guard stashes `(token, context)` under a private request-state
key; `authenticate_request` reuses it when the token matches. VALIDATION is cached;
AUTHORIZATION never is — `_check_requirement` still runs, so a cryptographically valid
but out-of-scope token is still refused with the canonical 403.

**Scope boundary.** Only the `Bearer` scheme is treated as an assertion. A `Basic`
`Authorization` passes through, because rejecting it is a policy REQ-8 does not state
and no repo caller exercises. A decision, not an oversight.

## Kong future-enable precondition

Kong is **not** in the current deployment denominator: ADR 0024 defers it, live inventory
reports `kong_gateway=''` and `kong_ip=''`, `MUSUBI_API_URL` is direct `:8100`, and
80/443 do not serve Musubi. `deploy/kong/musubi-prod.yml` is staged future-state and is
untouched by this slice.

**Before Kong is activated, verify that its forwarded OIDC access token satisfies Musubi
issuer/audience validation under REQ-8.** Its `musubi-api-v1` route matches `paths:
["/v1"]`, which includes `/v1/ops/*`, and it injects upstream via
`upstream_access_token_header`. A token Musubi cannot validate would now be a 401 where
it was previously ignored.

## Deployment boundary

The implementation shipped in Musubi `v1.23.4` and was deployed by the second seat to
both `core` and `lifecycle-worker` at the signed digest
`sha256:d77fd19c64ae6b9fd46e31ed36304ae03778b371cee7ff299eea9848abcb3b0c`.
No Hermes profile bytes changed during this deployment; the four deployed providers
remained byte-identical to their accepted canonical source.

## Closure evidence

Two distinct heads, deliberately labelled — they are not interchangeable:

- **Implementation head** `8bce44cbd875f158a60a18f77635a8b0c7daab87` — the exact head at
  which the source and tests were independently reviewed and every mutant reproduced by
  the second seat.
- **Handoff head** — this commit, which adds only the durable closure surface (this
  slice plus the two canonical status rows) on top of that implementation head. The
  docs-only delta between them is exactly three files.

Base `origin/main` `8d2c81d`.

Gate: `ruff format --check` 410 formatted; `ruff check` clean; `mypy src tests` clean
(395 files); `pytest --cov=musubi` 2633 passed, 195 skipped, 2 xfailed, 0 failed,
coverage 88.41%; `check.py all` clean (warnings only).

Discriminating red — four mutants, each failing exactly the test that names it:

| mutant | failures |
|---|---|
| remove the middleware registration | exactly 5; 9 controls green |
| disable the cached-context reuse | exactly 1 — double validation |
| bypass `_check_requirement` | exactly 1 — canonical authorization denial |
| restore the stale 401 hint | exactly 1 — truthful hint |

Independently reproduced at the IMPLEMENTATION head by the second seat.

Durable and live closeout:

- PR #673 merged as `0453a137cb8dbe39d7a7a9fb6505f75a94268f97`. Release
  PR #674 merged as `ca926473436bbdbc39b74e8e7085ecda348e16f0`; the signed
  `v1.23.4` image digest is
  `sha256:d77fd19c64ae6b9fd46e31ed36304ae03778b371cee7ff299eea9848abcb3b0c`.
  Auto-pin PR #675 merged as `0a646ba13410fbc017283973fcfc9a11500d1b7d`.
- The household deployment authority, `hw-ansible`, pinned `v1.23.4` in PR #14
  (`9afa2d1e81cedc65f37ca9e84141b51ac5e9e8de`). A stale local source comparison
  initially regressed the lifecycle-worker healthcheck; the second seat stopped on the
  resulting unhealthy container, restored exact Musubi `origin/main` template parity in
  hw-ansible PR #15 (`bc85419be246e752c8c7b2187752a390d6c287db`), and reran the
  canonical update. The corrected play completed `ok=18 changed=4 failed=0`, asserted
  both services on the pin, and appended the upgrade-history row.
- Direct runtime inspection showed `core` and `lifecycle-worker` both `healthy`, both
  running the signed digest above. `memory-data --json musubi status` returned
  `status: ok`, `version: v1.23.4`, and all five dependencies healthy.
- Live REQ-8 matrix on all five public routes (`/v1/ops/health`, `/v1/ops/status`,
  `/v1/ops/metrics`, `/v1/docs`, `/v1/openapi.json`): absent bearer `200`; deliberately
  invalid bearer canonical typed `401` with `code: UNAUTHORIZED` and non-empty detail;
  real valid bearer `200`. Protected `/v1/namespaces` remained canonical typed `401`
  for both absent and invalid controls.
- Repository smoke wrappers passed while supplied an intentionally invalid
  `MUSUBI_TOKEN`: API health plus all five components and both observability assertions
  were green, proving public probes use the credential-free `public_get` path.
- Nyla, Sumi, Shiori, and Tama each completed a value-free live recall through the
  deployed Hermes provider at SHA-256
  `2e8b3dd0a9bff4ae3fbc94e2185cfa0ae149fc6c5d3e9b2985cbb982fe9d5722`.
  Each used its configured namespace and real secret-bootstrap path, returned `ok`, and
  exposed `warnings: []`; no memory content or credential value was printed.

## Work log

- 2026-08-03 — Pre-code client survey required before implementation. Repo-owned health
  probes (`docker-compose.yml` healthcheck, `deploy/docker/smoke-health.sh`,
  `deploy/ansible/group_vars/all.yml`) send no `Authorization` at all, so absent-stays-public
  covers them. `deploy/smoke/lib.sh` DID present a bearer to all three public ops probes;
  split to an unauthenticated `public_get`. `AUTH_ARGS` deliberately NOT dropped globally —
  protected smoke calls keep the bearer so a stale credential still fails the auth portion
  loudly rather than reading as a service-health failure.
- 2026-08-03 — `/v1/docs` and `/v1/openapi.json` have no repo-owned smoke caller (verified
  by grep; test callers only), so no helper change there.
- 2026-08-03 — Ten `Authorization: Bearer fake` headers removed from
  `tests/api/test_ret007_*` and `tests/api/test_ret009_*`. Those suites monkeypatch
  `musubi.api.routers.*.authenticate_request` to return `Ok` and assert retrieval
  orchestration, not auth; the header was an invalid credential that only ever worked
  because auth was mocked at the router. Verified against clean `origin/main` that all 19
  affected tests passed BEFORE this change. Accepted in review as test-honesty repair.
- 2026-08-04 — First revision validated twice per protected request (`validate_token`
  call_count=2). `tokens.py` has no JWKS cache, so under RS256 that is two synchronous
  JWKS fetches per protected request. Fixed with the token-matched request-state seam.
- 2026-08-04 — First revision's 401 hint said "omit it to call a public route", false on a
  protected route where omission is also a 401. Corrected.
- 2026-08-04 — Two load-bearing assertions were too broad to prove their claims. `!= 401`
  on the valid-bearer case was *passing on a 403*, because the scope string used
  (`eric/*:rw`) does not grant that namespace — the request never reached the handler, so
  the validation count was taken from a refused request. `in (401, 403)` on the
  out-of-scope case could pass on a cryptographic rejection and so proved nothing about
  `_check_requirement`. Both replaced with exact expectations, including the literal
  canonical scope-denial detail.
- 2026-08-04 — REQ-7 (#412) and D4 Phase 1 (#558) untouched. Suite `xfailed` count 3 → 2,
  which is the mechanical proof REQ-7's strict-xfail still stands.
- 2026-08-04 — PR #673 merged without auto-closing #413. The second seat released and
  deployed `v1.23.4`, corrected a self-introduced lifecycle healthcheck regression in the
  deployment authority before accepting runtime state, then completed the public-route,
  protected-control, smoke-wrapper, and four-seat Hermes proofs recorded above.
