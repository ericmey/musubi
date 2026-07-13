---
title: "Slice: SEC-002 — idempotency replay bypasses authentication"
slice_id: slice-sec-002-idempotency-auth-bypass
section: _slices
type: slice
status: done
owner: aoi
phase: "Security audit 2026-07-12 (Eric, discoverer)"
tags: [section/slices, status/done, type/slice, security, p0, auth, idempotency]
updated: 2026-07-12
reviewed: true
issue: 407
depends-on: []
blocks: [slice-auth-boundary-red-contract, slice-idempotency-phase-b]
---

# SEC-002 (C1) — idempotency replay bypasses authentication  ·  P0

**Discoverer: Eric.** Source-confirmed by Yua (router). Red tests by Aoi.

## The vulnerability

`create_app`'s documented middleware order (`src/musubi/api/app.py:4`) runs the
**idempotency cache BEFORE authentication**:

```
1. Correlation ID
2. Idempotency cache   <- cache.lookup() here, returns a cached response
3. Rate limit
   ... auth runs inside call_next, AFTER the cache has already answered
```

And the cache binds **only the `Idempotency-Key` header + a body hash**
(`src/musubi/api/idempotency.py:59` `lookup(key, body)`) — **nothing about the caller's
identity**: not the bearer token, not the subject, not the namespace, not the
route/method.

So, once any write has populated the cache for `(key, body)`:

- an **unauthenticated** request with that key + body gets the cached success back
- a **different tenant's** token replays another tenant's write result
- the **same key + body on a different route/namespace** can collide

A cached 2xx is returned with `X-Idempotent-Replay: true` and no bearer is ever checked.

## Scope

**Red tests + design proposal only.** No production code until the security lane is
approved. `src/musubi/**` is FORBIDDEN in this slice.

`owns_paths`:
- `tests/api/test_sec002_idempotency_auth.py`
- `docs/Musubi/_slices/slice-sec-002-idempotency-auth-bypass.md`

`forbidden_paths`:
- `src/musubi/**` (esp. `src/musubi/api/`, `src/musubi/auth/`, `openapi.yaml`) — frozen;
  the fix is an ADR-gated change owned by `slice-api-v*`

## Specs to implement

- [[_slices/slice-sec-002-idempotency-auth-bypass]] — closed by PR #404 (Phase B); the numbered Test Contract below resolves at #404 head (all passing).

## Test Contract (Yua's required cases)

Closure (numbered, resolve at #404 head — all passing; `make tc-coverage` exit 0):
1. `test_no_bearer_must_not_replay` no-bearer replay → 401 (SEC-002).
2. `test_invalid_bearer_must_not_replay` invalid-bearer replay → 401 (SEC-002).
3. `test_cross_tenant_must_not_replay` cross-tenant replay → 403, no disclosure (SEC-002).
4. `test_owner_can_replay_its_own_write` owner replay preserved (control).


Red tests, currently `xfail(strict=True)` — they assert the SECURE behaviour, so they
fail today and will pass once the fix lands. Do NOT use live sensitive content.

- [ ] missing bearer + known key + same body **must NOT replay** (expect 401, get cached 2xx)
- [ ] invalid bearer + known key + same body **must NOT replay**
- [ ] different-tenant valid bearer + known key + same body **must NOT replay** another tenant's result
- [ ] same key + same body across **different routes/namespaces must NOT collide**
- [ ] the **authenticated original subject CAN replay** its own write (idempotency still works for its owner)
- [ ] concurrent miss (H1 / IDEM-001) — two parallel first-requests must not both mutate — *tracked with SEC-002 design, separate red test*

## Proposed invariant (design before code — Yua)

1. **Authenticate BEFORE idempotency lookup.** Move auth ahead of the cache in the
   middleware chain, or perform token validation inside the idempotency middleware
   before `lookup`.
2. **Bind the cache identity to the validated caller**, not just `(key, body)`:
   `subject / token-id + normalized route + method + authorized namespace + key`.
3. Do this **without double-consuming** the request body / multipart stream (the body is
   already read once for the hash; the fix must not break that).

Breaking vs additive: middleware-order change is internal; the cache-key change is
internal to `IdempotencyCache`. No wire/API schema change expected → **not** an API
version bump, but it IS an auth-path change and needs the security-lane ADR.

## Status
Red tests written and failing (documenting the hole). No fix. Awaiting security-lane
approval and the auth-boundary owner.

## Lane disposition (2026-07-12)
Canonical lane is now [[_slices/slice-auth-boundary-red-contract]] (branch `slice/adr-auth-boundary`),
which consolidates and RUNS this slice's reds — this slice is a live dependency of it, not
superseded. The standalone branch `slice/sec-002-idempotency-auth-bypass` (tip `3ca89bc`) is a
direct ANCESTOR of the consolidated branch: **0 unique commits, nothing to cherry-pick.**
Retire-pending; do not delete yet (per Yua process-hygiene REQ 21:52).
