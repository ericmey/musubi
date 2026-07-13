---
title: "ADR: consolidated auth boundary — SEC-002/003/004 + IDEM-001"
type: adr
status: proposed
owner: aoi
discoverer: eric
reviewed_by: yua
phase: "Security audit 2026-07-12"
tags: [type/adr, security, auth, idempotency, proposed]
updated: 2026-07-12
supersedes: []
---

# ADR — Consolidated auth boundary: one authentication step, route-native authorization, identity-bound idempotency

**Discoverer of all four defects: Eric.** Source-confirmed and routed by Yua. Design: Aoi.
**Status: PROPOSED — no `src/` changes until this is approved and slice ownership assigned.**

This ADR consolidates four confirmed defects that share ONE root cause: **authorization
decisions are made against inputs the auth layer cannot reliably see, and one of them runs
before authentication happens at all.**

| ID | Defect | Root cause |
| --- | --- | --- |
| SEC-002 | idempotency replay bypasses auth | idempotency is a *middleware* (runs before `call_next`); auth is a per-route *dependency* (runs inside `call_next`). Replay is served before auth. Cache binds only `(key, body-hash)`. |
| SEC-003 | Form/Path namespace bypasses scope | `require_auth` reads namespace **only** from `request.query_params` (`auth.py:48`). Form/Path/Body namespaces are invisible → `ns=None` → scope check defanged. |
| SEC-004 | contradictions omitted-namespace fleet scroll | route is `require_auth()` (ordinary) not `require_operator()`; omitted namespace → unfiltered scroll of all tenants. |
| IDEM-001 | concurrent-miss double execution | idempotency lookup/store is not transactional across miss→execute→store; two parallel first-requests can both mutate. |

## Current architecture (verified in source, 2026-07-12)

```
REQUEST
  │
  ├─ correlation_id_middleware        (app.py:193)      mints X-Request-Id
  │
  ├─ rate-limit + idempotency mware    (app.py:225)
  │     ├─ idem_key = headers[Idempotency-Key]           (app.py:241)
  │     ├─ cache.lookup(idem_key, body_hash)             (app.py:249)  ◀── SEC-002: NO AUTH YET
  │     │     hit → return cached response  ─────────────────────────┐
  │     └─ (miss) → call_next(request) ──────┐                       │
  │                                          │                       │
  │                              ┌───────────▼─────────────┐         │
  │                              │  ROUTE (FastAPI Depends) │         │
  │                              │  Depends(require_auth()) │  ◀── auth is HERE, per-route
  │                              │   ns = query_params[...] │  ◀── SEC-003: query-only
  │                              │   authenticate_request() │
  │                              │   handler(...)           │  ◀── SEC-004: omit → fanout
  │                              └───────────┬─────────────┘         │
  │                                          │                       │
  │     ┌──── cache.store(key, body, resp) ◀─┘                       │
  │     ▼                                                            │
  └─ response ◀──────────────────────────────────────────────────────┘
```

**The two structural facts:** (1) authentication is a per-route dependency, so it lives
*inside* `call_next` — the idempotency middleware wraps it from outside and can answer
first. (2) `require_auth` sources the namespace from exactly one place (`query_params`),
so any route that carries the namespace elsewhere authorizes `None`.

Adjacent smell (not one of the four, flag only): `_operator_scope_hint` (`app.py:114`)
base64-decodes the JWT payload **without verifying the signature** to pick a rate-limit
tier. Low impact (tier only), but a forged token gets the operator *rate ceiling*. Note in
the ADR; fix out of scope.

## Decision

Three coordinated changes, all inside the auth/api boundary:

### D1 — ONE canonical authentication step, before idempotency

Introduce a single **authentication middleware** that runs **before** the idempotency
middleware and establishes `request.state.principal` = the validated
`{subject, token_id (jti), scopes, presence}`. Authentication (is this a real, unexpired,
correctly-signed token) is separated from **authorization** (may this principal do this
operation on this namespace) — authn is global and early; authz stays route-native (D2).

Rejected alternative: keep auth per-route and just reorder the idempotency middleware after
it. Not possible — the idempotency middleware cannot run *after* a per-route dependency;
middleware is strictly outside `call_next`. Hence authn must become middleware.

### D2 — Route-native authorization from PARSED values

`require_auth` stops guessing the namespace from `query_params`. Instead the route hands
authorization the **already-parsed** namespace from wherever it actually lives:

- JSON body → the parsed model field
- multipart `Form(...)` → the parsed form field (see multipart-timing below)
- `Path(...)` → the parsed path value
- `Query(...)` → the query value
- omitted/nullable → an **explicit fanout contract**: either `require_operator`, or a
  documented authorized-fanout dependency; never an implicit all-tenant scroll (SEC-004)

Mechanism: an authorization dependency `authorize(access, namespace)` that receives the
resolved namespace as an argument, plus per-source helpers (`namespace_from_form`,
`namespace_from_path`, `namespace_from_body`) so no route hand-rolls extraction. The
generic "read it from the query" default is deleted.

**Multipart parse-timing:** the Form namespace must be parsed *before* the file stream is
consumed, and without consuming the upload into memory twice. FastAPI parses `Form(...)`
and `UploadFile` from the same multipart stream; the authorization dependency must read the
`namespace` Form field (small, early in the part order by convention) and let the
`UploadFile` remain a lazy stream. Design note: require the `namespace` part to precede the
`file` part, or buffer only the small form fields. This is the one genuinely fiddly piece
and needs an implementation spike before the fix lands.

### D3 — Idempotency replay only after an authorization equivalent to the uncached op

A replay is served **only if** the current request passes the *same* authentication AND
the *same* authorization the uncached operation would require. Cache identity is
**defense-in-depth**, not the gate.

Comparison of the three binding strategies Yua asked for:

| strategy | binds | pros | cons | verdict |
| --- | --- | --- | --- | --- |
| (A) exact-token fingerprint (HMAC of the bearer) | one specific token | simplest; a rotated/expired token can't replay | breaks legit replay across a token refresh; ties cache to a secret's lifetime | **reject as the gate** — use only as an optional extra key |
| (B) principal + re-authorize | subject/jti + re-run authz | survives token rotation for the same principal; correct authorization semantics | must re-evaluate authz on replay (small cost) | **adopt** |
| (C) move replay behind route auth dependency | route decides | authz is exactly the uncached path's | idempotency is a cross-cutting concern; per-route wiring is error-prone and easy to forget on a new route | **reject** — reintroduces the "new route hides the bug" risk |

**Decision: (B), centrally.** After D1 authenticates and D2 authorizes, the idempotency
layer keys the cache on `{principal.subject, jti-or-token-rotation-safe-id, normalized
route template, method, authorized namespace, Idempotency-Key}` and **replays only to a
request that passes the current authz**. A cross-tenant or unauthenticated request never
reaches lookup with a matching identity, so it is refused (401/403) before any cached body
is disclosed.

### D4 — IDEM-001 in-flight ownership

The miss→execute→store window becomes a single owned transaction: on miss, insert an
**in-flight marker** keyed by the same identity tuple under a lock/CAS; a concurrent second
request with the same identity either waits for the first result or gets a definitive
"in progress" (409/425), never a second execution. Cross-process durability is required
(the current cache is process-local — see migration).

## Request sequences (post-fix)

```
JSON write:     authn-mw → idem(identity+authz) → route(require_auth on body ns) → handler
Multipart:      authn-mw → idem(identity+authz) → route(authorize on parsed Form ns) → handler
Path stats:     authn-mw → route(authorize on parsed Path ns)          (GET, no idem)
Query read:     authn-mw → route(authorize on Query ns)                (unchanged behaviour)
Nullable fanout: authn-mw → route(require_operator OR explicit fanout contract) → scroll
```

## Error / status compatibility

- unauthenticated → **401** (today: sometimes a cached 2xx — SEC-002)
- authenticated, unauthorized namespace → **403** (today: 200 fleet scroll / defanged write)
- cross-tenant replay → **403**, and **no cached body/object_id disclosed**
- backend (Qdrant) failure → **5xx**, never an empty 200 (SEC-004/RET-007 folds in here)
- legitimate owner replay → **2xx + `X-Idempotent-Replay: true`** (unchanged)
- concurrent miss → first wins 2xx; second **409/425**, never double-mutates

No response *schema* changes → **not** an API version bump. It IS an auth-path behaviour
change → security-lane ADR + changelog + a compatibility note for any client currently
(accidentally) relying on the permissive behaviour.

## Migration / cache invalidation

- The idempotency cache is currently **process-local** (`IdempotencyCache`, in-memory).
  Identity-bound replay + IDEM-001 cross-process ownership need a **shared** store
  (the same one Musubi already runs for durable state) or an explicit single-writer
  contract. This is the largest implementation item.
- On deploy, **invalidate the existing cache** — old entries are keyed on `(key, body)`
  with no identity and must not be replayed under the new rules. A cold cache is safe (a
  miss just re-executes idempotently for its owner).

## Observability (no token / PII)

Emit on every auth decision and every replay: `event`, `decision` (allow/deny),
`reason_code`, `subject` (namespace-prefix only, already logged this way in
`auth.scopes`), `route_template`, `method`, `namespace`, `idempotent_replay` bool,
`in_flight_conflict` bool. **Never** the bearer, the jti raw, or request/response bodies.
This is also the metric that would have made SEC-002/003/004 *visible* — a deny-rate or a
cross-namespace-scroll counter, which today does not exist.

## Test closure matrix (every red + control maps to a change)

| test | file | today | closed by |
| --- | --- | --- | --- |
| no-bearer replay → 401 | test_sec002 | XFAIL | D1 (authn before idem) |
| invalid-bearer replay → 401 | test_sec002 | XFAIL | D1 |
| cross-tenant replay → 403 + no disclosure | test_sec002 | XFAIL | D1+D3 |
| owner replay → 2xx replay | test_sec002 | PASS | D3 (preserve) |
| collision cross-route | test_sec002 | SKIP (unreproduced) | D3 route-template key (revisit) |
| upload cross-tenant Form ns → 403 | test_sec003 | XFAIL | D2 (form helper) |
| upload own ns → 2xx | test_sec003 | PASS | D2 (preserve) |
| stats cross-tenant Path ns → 403 | test_sec003 | XFAIL | D2 (path helper) |
| stats own ns → 2xx + shape | test_sec003 | PASS | D2 (preserve) |
| both no-token → 401 | test_sec003 | PASS | D1 (preserve) |
| ordinary + omitted ns → 403 | test_sec004 | XFAIL | D2 (fanout contract) |
| operator + omitted → 2xx cross-ns | test_sec004 | PASS | D2 (preserve) |
| ordinary own/foreign ns | test_sec004 | PASS | D2 (preserve) |
| backend failure → 5xx | test_sec004 | XFAIL | error-handling (RET-007) |
| concurrent miss → no double-mutate | (IDEM-001, to write) | — | D4 |

## Ownership / process

- **Slice ownership:** this touches `src/musubi/api/` + `src/musubi/auth/` — the frozen
  API/auth boundary. Requires a `slice-api-v*` (or a dedicated `slice-auth-boundary`) owner,
  NOT this observation slice. Aoi's slices are red-tests-only.
- **Issues/ADR:** this doc is the ADR; open one tracking Issue per defect linked to it,
  plus one "auth-boundary refactor" epic. Multipart parse-timing gets its own spike ticket.
- **Deploy / rollback:** ship behind a feature flag (`auth_boundary_v2`) defaulting off in
  staging first; the compatibility risk is legitimate clients relying on permissive
  behaviour. Rollback = flag off + cache flush. Because a cold cache is always safe, the
  idempotency change is low-rollback-risk; the authz change is the one to stage carefully.

## What this ADR does NOT decide (deferred)

- The signature-unverified `_operator_scope_hint` (flag only; separate ticket).
- The DQ-001 projection/summary contract (separate lane).
- The lifecycle/atomicity work (LIFE-007/008, DATA-001) — different root cause, different
  ADR.

---

**Bring to Yua before any code.** No `src/` change is authorized by this document; it is a
proposal for the security lane and the auth-boundary slice owner.
