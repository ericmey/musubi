---
title: "Slice: Auth middleware"
slice_id: slice-auth
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "1 Schema"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-config]]"]
blocks: ["[[_slices/slice-api-v0-read]]", "[[_slices/slice-api-v0-write]]"]
---

# Slice: Auth middleware

> Bearer token validation + namespace scope check + optional mTLS. Sits as middleware; business logic never parses auth headers.

**Phase:** 1 Schema · **Status:** `done` · **Owner:** `codex-gpt5`

## Specs to implement

- [[10-security/auth]]

## Owned paths (you MAY write here)

- `musubi/auth/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/api/routes/`
- `musubi/planes/`

## Depends on

- [[_slices/slice-config]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-api-v0-read]]

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 10:38 — codex-gpt5 — claim

- Claimed slice via Issue #7 and branch `slice/slice-auth`.

### 2026-04-19 10:38 — codex-gpt5 — handoff to in-review

- Landed Core auth middleware surface: HS256/RS256 JWT validation with JWKS lookup, namespace/special-scope checks, blended-query plane expansion, and FastAPI-compatible request auth context attachment.
- Added `pyjwt[crypto]` because RS256/JWKS verification needs vetted RSA/JWT primitives rather than a hand-rolled stdlib verifier.
- Tests: `make check` passed with 230 passed / 4 skipped; Test Contract coverage is 10 passing and 4 skipped for `slice-auth-authority` follow-up (`test_pkce_flow_end_to_end`, `test_refresh_token_rotation_issues_new_refresh`, `test_revocation_invalidates_token_within_60s_cache`, `test_operator_issued_only_via_cli`).
- Coverage: auth package 98.40% branch/line combined on focused `musubi.auth` coverage run; repo total 96.30%.
- `make agent-check` exited clean with pre-existing vault warnings only.

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- PR #38 — `feat(auth): slice-auth`
