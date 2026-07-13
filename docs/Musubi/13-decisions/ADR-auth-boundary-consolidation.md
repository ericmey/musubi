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

# ADR — Consolidated auth boundary: authenticate early, authorize + replay AFTER parsed values

**Discoverer of all four defects: Eric.** Source-confirmed and routed by Yua. Design: Aoi.
**Status: PROPOSED (rev 2) — no `src/` changes until approved and a slice owner is assigned.**

## Rev 2 — the contradiction Yua caught, and its resolution

Rev 1 said "idempotency replay only after authorization" (D3) **and** "authorization is
route-native, from parsed Form/Path/Body" (D2) — while the diagram still ran the
idempotency **middleware** before the route parses anything. **A middleware cannot use a
decision that has not happened yet without duplicating extraction + authz.** That is not
executable. Rev 1 is withdrawn on this point.

**Resolution: replay is no longer a middleware. It moves BEHIND the parsed-authz
dependency**, where the namespace has been parsed and authorization has run. Verified
feasible in this codebase:

- FastAPI **0.136** — `APIRoute` is subclassable (option 2 below is available).
- The app already registers `add_exception_handler(APIError, ...)` (`app.py:323`) — a typed
  replay short-circuit via exception is available (option 1).

## Current architecture (verified in source, 2026-07-12)

- **Authentication is a per-route `Depends(require_auth())`** → it runs *inside* `call_next`.
- **Idempotency + rate-limit is a middleware** (`app.py:225`, "around `call_next`") → it runs
  *outside*, and `cache.lookup` (`app.py:249`) answers **before any auth**. (SEC-002; the
  SEC-002 xfail suite proves it empirically.)
- `require_auth` reads the namespace **only** from `request.query_params` (`auth.py:48`) →
  Form/Path/Body namespaces authorize `None`. (SEC-003)
- Omitted-namespace + ordinary `require_auth()` → unfiltered fleet scroll. (SEC-004)
- The middleware calls `await request.body()` (`app.py:244`) to hash — **materializing the
  entire upload into memory, with no size limit anywhere.** (DoS surface; see D5.)

Adjacent smell (flag only, separate ticket): `_operator_scope_hint` (`app.py:114`)
base64-decodes the JWT **without verifying the signature** to pick a rate-limit tier.

## Decision

### D1 — one canonical AUTHENTICATION step, early

An authentication middleware (or ASGI layer) runs first and sets
`request.state.principal = {subject, presence, issuer, scopes}` for a valid, unexpired,
correctly-signed token — else 401. This is **authentication only** (who are you), not
authorization (may you do this). It has everything it needs from the bearer alone, so it
can run before any body/route parsing. SEC-002's "replay before auth" cannot occur because
there is no route-independent replay step anymore (see D3).

### D2 — route-native AUTHORIZATION from PARSED values

Authorization runs as a **shared route dependency** that receives the **already-parsed**
namespace from its real source, via per-source helpers:

```
authorize(access, namespace_source)   where namespace_source ∈
  { from_query, from_path, from_body_field, from_form_field }
```

Omitted/nullable namespace is an **explicit fanout contract**: `require_operator`, or a
named authorized-fanout dependency — never an implicit all-tenant scroll. The generic
"read the query" default is deleted.

**Enforcement is mechanical, not per-route discipline.** A CI check (extending
`tests/api/sec003_route_inventory.py`) fails the build if any write/read route with a
namespace lacks an explicit authorization source. Yua's point stands: per-route
declaration must not be hand-rolled logic a new route can forget.

### D3 — idempotency replay is a DEPENDENCY AFTER authz, not a middleware

This is the correction. The replay check becomes a dependency that runs **after** D2 has
authorized the parsed request:

```
Depends(authenticate)              # D1: principal on request.state, or 401
  → Depends(authorize(...))        # D2: parsed namespace, or 403
    → Depends(idempotent_replay)   # D3: identity+op known HERE; lookup; short-circuit if hit
      → handler                    # miss: execute, then store keyed by identity+op
```

`idempotent_replay` has, at this point, the authenticated principal AND the authorized
operation. It keys the cache on the **identity tuple** (D6) + normalized route template +
method + authorized namespace + `Idempotency-Key`, looks up, and:
- **hit** → short-circuits with the cached response (typed exception caught by the existing
  handler, or the custom-APIRoute return path).
- **miss** → the handler runs; the response is stored under the same identity key.

Because this dependency only ever runs *after* authn+authz, an unauthenticated or
cross-tenant request is refused (401/403) and **never reaches lookup**. Cache identity
(D6) is defense-in-depth on top of that, not the gate.

**Two implementation shapes to compare in the spike:**

| | (1) shared dependency + typed replay exception | (2) custom `APIRoute` pipeline |
| --- | --- | --- |
| mechanism | `Depends(idempotent_replay)` raises `IdempotentReplay(response)`, caught by an exception handler | subclass `APIRoute`; run authn→authz→replay in `get_route_handler`, return before handler |
| pros | minimal; uses existing exception-handler infra; explicit per-route deps are greppable | central, uniform, cannot be forgotten on a new route |
| cons | must be declared on every idempotent route (mitigated by the D2 CI check) | more machinery; harder to unit-test in isolation; changes routing internals |
| testability | high (dependency is directly callable) | medium |

**Recommendation: (1)** with the D2 CI enforcement, because it is smaller and directly
testable; escalate to (2) only if the CI check proves insufficient in practice.

### D4 — IDEM-001 concurrency, PHASED

Yua is right that the shared store is unspecified and **Qdrant is not a lock/CAS store.**
Phase it:

- **Phase 0 (P0 fix, ships first):** secure authn/authz/replay (D1–D3, D6) **plus
  in-process atomic ownership** of the miss→execute→store window, **enforced under a
  single-worker deployment**. A concurrent same-identity miss in one process waits on an
  in-process lock or gets 409/425 — never double-executes. This closes the P0 without a new
  datastore.
- **Phase 1 (separate decision):** durable **cross-process** ownership for multi-worker.
  Requires choosing a real CAS/lock store (Postgres advisory locks / Redis / etc. — a
  distinct ADR). Do NOT ship multi-worker idempotency until Phase 1 lands.

### D5 — bound the body materialization (DoS)

The hash step must not read an unbounded upload into memory. Enforce a **max body size**
before hashing (413 over the limit), and define a **canonical hash** that does not require
spooling a large multipart file into memory — e.g. hash only the small structured fields +
a streamed digest of the file part, or exempt large multipart uploads from body-hash
idempotency and rely on the identity+route+key tuple. Multipart **part order cannot be a
security convention** (FastAPI parses/spools independently); the design must not depend on
`namespace` arriving before `file`. Spike item.

### D6 — identity is issuer + subject + presence (NOT jti)

Yua's correction: **`jti` is per-token and rotates; it is not rotation-stable.** The cache
identity is `(issuer, subject, presence)` so a legitimate token refresh for the same
principal still replays its own write. `jti` may be recorded in structured logs for audit
but is **not** part of the cache key. Chosen explicitly.

This also resolves the rev-1 strategy comparison: "exact-token fingerprint" (A) is rejected
precisely because it keys on the rotating secret; **(B) principal (issuer+subject+presence)
+ re-authorize** is adopted; per-route replay (C) is rejected.

## Executable request sequences (post-fix)

```
JSON write:
  authn-mw(401?) → route: Depends(authorize(from_body_field "namespace"), "w")(403?)
                        → Depends(idempotent_replay)(hit→replay | miss→) → handler → store

Multipart upload:
  authn-mw(401?) → route: parse Form("namespace") → Depends(authorize(from_form_field,"w"))(403?)
                        → Depends(idempotent_replay)(bounded-hash, D5) → handler → store

Path stats (GET, no idem):
  authn-mw(401?) → route: Depends(authorize(from_path "namespace_path", "r"))(403?) → handler

Query read (unchanged behaviour):
  authn-mw(401?) → route: Depends(authorize(from_query "namespace", "r"))(403?) → handler

Nullable fanout (contradictions):
  authn-mw(401?) → route: Depends(require_operator OR authorized_fanout)(403 if ordinary+omitted)
                        → scroll (filtered per contract)
```

Every arrow is a real FastAPI dependency edge that runs in that order — no step consumes a
decision that has not yet been made.

## Error / status compatibility

401 unauth · 403 unauthorized namespace / ordinary+omitted fanout · cross-tenant replay 403
with **no cached body/object_id disclosed** · backend (Qdrant) failure **5xx, never empty
200** (folds SEC-004/RET-007) · owner replay **2xx + `X-Idempotent-Replay: true`** · concurrent
same-identity miss → first 2xx, second **409/425** · oversize body **413** (D5). No response
*schema* change → not an API version bump; it IS an auth-path behaviour change → ADR +
changelog + client-compat note.

## Migration / cache invalidation

Existing cache entries are keyed `(key, body)` with no identity → **must be invalidated on
deploy** (a cold cache is safe; a miss re-executes idempotently for its owner). Phase-0
in-process store is fine under single-worker; Phase-1 chooses the durable cross-process
store.

## Observability (PII-safe, corrected per Yua)

Metrics carry **bounded labels only**: `decision` (allow/deny), `reason_code`,
`route_template`, `method`, `idempotent_replay` bool, `in_flight_conflict` bool. **No raw
subject / namespace / route id / token as a metric label** (cardinality + PII). Raw subject
and `jti` appear **only in structured logs**, pseudonymous where possible. This is also the
deny-rate / cross-namespace-scroll signal that would have made SEC-002/003/004 visible —
which does not exist today.

## Test closure matrix

| test | today | closed by |
| --- | --- | --- |
| no-bearer / invalid-bearer replay → 401 | XFAIL (sec002) | D1 + D3 (no pre-auth replay path) |
| cross-tenant replay → 403 + no disclosure | XFAIL (sec002) | D3 + D6 |
| owner replay → 2xx replay | PASS (sec002) | D3 + D6 (preserve) |
| collision cross-route | SKIP (unreproduced) | D3 route-template key (revisit) |
| upload cross-tenant Form ns → 403 | XFAIL (sec003) | D2 from_form_field |
| stats cross-tenant Path ns → 403 | XFAIL (sec003) | D2 from_path |
| own-ns upload/stats → 2xx (+shape) | PASS (sec003) | D2 (preserve) |
| no-token routes → 401 | PASS (sec003) | D1 (preserve) |
| ordinary + omitted ns → 403 | XFAIL (sec004) | D2 fanout contract |
| operator + omitted → 2xx cross-ns | PASS (sec004) | D2 (preserve) |
| own/foreign ns explicit | PASS (sec004) | D2 (preserve) |
| backend failure → 5xx | XFAIL (sec004) | error-handling (RET-007) |
| concurrent miss → no double-mutate | to write (IDEM-001) | D4 Phase 0 |
| oversize body → 413 | to write | D5 |

## Ownership / process (Yua's proposal, adopted)

- **Implementation owner:** Aoi, **after approval** — on a `slice-auth-boundary` (or
  `slice-api-v*`) slice, not this red-tests-only observation slice.
- **Acceptance gate:** Yua.
- **Security / runtime re-review:** Tama + Shiori.
- **Merger:** a different party than the implementer.
- **Issues/ADR:** this doc (canonical `docs/Musubi/13-decisions/`); one tracking Issue per
  defect + an "auth-boundary" epic; the multipart-hash/DoS spike (D5) and the durable-store
  decision (D4 Phase 1) each get their own ticket.
- **Deploy / rollback:** feature-flag may stage OFF in staging, but **production completion
  requires the secure path ON.** A rollback that reopens a P0 is **emergency-only**, not a
  routine toggle.

## Deferred (flagged, not decided here)

`_operator_scope_hint` signature-unverified rate-tier read; DQ-001 projection/summary;
LIFE-007/008 / DATA-001 atomicity (different root cause, different ADR).

---

**Bring to Yua before any code.** No `src/` change is authorized by this document.
