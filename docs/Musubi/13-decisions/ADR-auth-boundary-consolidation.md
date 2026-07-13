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
**Status: PROPOSED (rev 3) — no `src/` changes until approved and a slice owner is assigned.**

## Rev 3 — split pipeline (Yua's "option 1 MODIFIED"), every step PROVEN executable

Yua's correction to rev 2: a dependency can lookup/lock/replay *before* the handler, but it
**cannot capture the serialized handler response afterward** — storing the response needs a
step that runs *after* `call_next`. So the pipeline splits: the **security decision** is a
dependency (pre-handler); the **store/release** is a thin outer middleware (post-handler)
that does NO lookup, authz, or body read — it only reads `request.state` and caches a
successful response / releases ownership.

**The FOUR PIPELINE claims below were proven against a live Starlette/FastAPI 0.136 app
before writing this rev** (not asserted). **The multipart canonical-digest (D5) is NOT yet
proven — it is an explicit spike item (see D5).** Scope the word "proven" to exactly these
four:

1. an **outer middleware sees `request.state` set by a route dependency**, after
   `call_next` → the split is possible. ✓ proven
2. a **typed `Replay` exception raised in the dependency reaches the middleware AS a
   response** (via the existing exception-handler infra), carrying
   `X-Idempotent-Replay: true`. ✓ proven
3. the middleware can **distinguish hit vs miss and read the status** to decide what to store.
   ✓ proven — BUT **streaming detection is UNSETTLED**: the spike disproved `isinstance`
   (everything is wrapped as `_StreamingResponse` under BaseHTTPMiddleware), and my
   follow-on "absence of Content-Length" guess is ALSO only a heuristic (Yua: a buffered or
   transform-middleware response may omit Content-Length; a StreamingResponse may set it;
   header manipulation could make a stream look cacheable). **No proven stream detector yet
   — see D3 cacheability contract.** Do not treat streaming detection as solved.
4. **the store gate must be `2xx && non-streaming && non-replay`, NOT try/except** — because
   `HTTPException(500)` is converted to a **500 response**, not propagated as an exception,
   so an exception-keyed gate would cache a 500. Ownership is released in `finally` (always;
   success, error, or cancel), cache written only on success. ✓ proven (a bug I would have
   shipped, caught by running it)

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

### D3 — SPLIT pipeline: security decision in a dependency, store/release in a thin middleware

The replay check is a **dependency** (it can short-circuit before the handler); the store is
a **thin outer middleware** (only it can see the serialized response after `call_next`).
Neither the middleware does authz/lookup, nor the dependency captures the response.

```
[outer store middleware]  ← post-handler ONLY: read request.state, cache on 2xx success, release ownership
  └─ [authn middleware]   D1: validate any presented bearer, attach principal (or 401 on protected routes)
       └─ route:
          Depends(authorize(parsed namespace, access))         D2: 403 if unauthorized
            → Depends(idempotent_replay)                        D3: identity+op known HERE
                 hit  → raise Replay(cached_response)   ─┐  (typed, caught by exception handler)
                 miss → acquire in-flight, write authorized idem_ctx to request.state
                   → handler                              │
          (Replay/handler response flows back OUT) ───────┘
  ← store middleware: if request.state.idem_ctx AND 2xx AND not streaming AND not replay → cache; ALWAYS release
```

**(a) Optional early authn middleware (D1).** Validates a *presented* bearer and attaches
`request.state.principal`. It does NOT force auth — **public routes with no bearer stay
public** (health/docs); protected routes require the principal via their authz dependency.
This avoids the "global authn 401s `/health`" failure Yua flagged. Explicit public-route
list is part of the spec (health, docs, openapi.json, metrics if public).

**(b) Parsed route-native authz (D2).** As above; **consumes** the validated
`request.state.principal` — `require_auth`/`authorize` does NOT re-decode or re-validate the
token, only checks scope against the parsed namespace.

**(c) Shared idempotency dependency (D3).** Runs after authz, so identity + authorized
operation are known. Computes the canonical hash (D5), checks the cache / acquires in-flight
ownership. **Hit** → `raise Replay(response)` (typed; caught by the existing `APIError`-style
handler → becomes a real response). **Miss** → writes `request.state.idem_ctx =
{identity_key, in_flight_token}` and proceeds to the handler.

**(d) Thin store middleware.** After `call_next`: if `request.state.idem_ctx` is set AND the
response is **2xx AND non-streaming AND not a replay**, store it under the identity key. It
performs **no lookup, no authz, no body read**. Ownership is released in a `finally` on
**every** path — success, error response, or client cancel — so a crashed/cancelled request
never leaves a stuck in-flight marker and never caches a failed or streaming response.

**Streaming / cacheability is an EXPLICIT CONTRACT, not a header heuristic (Yua, UNSETTLED).**
`isinstance(StreamingResponse)` is disproven (everything wraps to `_StreamingResponse` under
BaseHTTPMiddleware); "absence of Content-Length" is ALSO only a heuristic (buffered/transform
responses may omit it; a stream may set it; a header can be manipulated). So cacheability is
NOT inferred from the response shape. Two candidate mechanisms for the spike to settle:
  (i)  a **route-level cacheability declaration** — only routes explicitly registered as
       idempotent-JSON are cache-eligible; anything else (streams, SSE, downloads) is never
       cached regardless of headers; OR
  (ii) an **ASGI send-wrapper** that observes `http.response.body`'s `more_body` flag
       directly (the true streaming signal at the protocol level) without lossy Response
       reconstruction.
Adversarial tests REQUIRED before this is proven: buffered 2xx WITHOUT Content-Length stays
eligible if the route contract says cacheable; a stream WITH an explicit Content-Length is
NEVER cached; GZip/transform middleware; 204; multi-frame bodies; background-task + duplicate
headers. Until those pass, claim 3's streaming half is UNSETTLED.

**Why the gate is status-based, not exception-based (proven):** `HTTPException(500)` is
converted by FastAPI into a **500 response**, not propagated as an exception, so a
try/except store gate would cache the 500. The gate keys on `2xx && non-streaming &&
not-replay`; release is unconditional (`finally`).

Because D3 runs only after D1+D2, an unauthenticated or cross-tenant request is refused
(401/403) and **never reaches lookup**. Cache identity (D6) is defense-in-depth on top.

**Shape chosen: option 1 MODIFIED (dependency + thin store middleware).** The custom
`APIRoute` option is **rejected unless a spike proves** it can insert between dependency
resolution and the endpoint without reimplementing FastAPI internals — a bar the split
pipeline clears today with proven primitives.

### D4 — IDEM-001 concurrency, PHASED

Yua is right that the shared store is unspecified and **Qdrant is not a lock/CAS store.**
Phase it:

- **Phase 0 (P0 fix, ships first):** secure authn/authz/replay (D1–D3, D6) **plus
  in-process atomic ownership** of the miss→execute→store window, **enforced under a
  single-worker deployment**. A concurrent same-identity miss in one process waits on an
  in-process lock or gets 409/425 — never double-executes. This closes the P0 without a new
  datastore. **Single-worker is not a comment — it is enforced and CONFIG-TESTED:** a
  startup assertion (or a test that fails if `workers > 1` while multi-process idempotency
  is unimplemented) so the safety assumption cannot silently regress when someone scales the
  deployment.
- **Phase 1 (separate decision):** durable **cross-process** ownership for multi-worker.
  Requires choosing a real CAS/lock store (Postgres advisory locks / Redis / etc. — a
  distinct ADR). Do NOT ship multi-worker idempotency until Phase 1 lands.

### D5 — bound the body materialization (DoS)

The hash step must not read an unbounded upload into memory. Enforce a **max body size
per route** (not a single global limit — an upload route and a small JSON write have
different legitimate ceilings; 413 over the route's limit), and define a **canonical hash**
that **still distinguishes two different files** without holding the whole upload in RAM —
a **streamed digest** of the file part (chunked SHA-256) combined with a hash of the small
structured fields. (Yua: the digest must distinguish different files; simply exempting the
file from the hash would let two different uploads with the same form fields collide on the
same idempotency key.) Multipart **part order cannot be a security convention** (FastAPI
parses/spools independently); the design must not depend on `namespace` arriving before
`file`. **STATUS: UNPROVEN.** Unlike the D3 pipeline claims, this digest design has NOT been
prototyped — the streaming-chunked-SHA-256 + rewind/spool + per-route-limit behaviour and
the "different files cannot collide" property must be demonstrated in a runnable spike
before the D5 fix is authorized. Rev 3 does not claim it proven.

### D6 — identity is issuer + subject + presence (NOT jti)

Yua's correction: **`jti` is per-token and rotates; it is not rotation-stable.** The cache
identity is `(issuer, subject, presence)` so a legitimate token refresh for the same
principal still replays its own write. `jti` may be recorded in structured logs for audit
but is **not** part of the cache key. Chosen explicitly.

**Validate the tuple's internal consistency** (Yua): `issuer`, `subject`, and `presence`
must be mutually consistent per the token contract — e.g. `presence` must be the
subject's declared presence, not an arbitrary claim — so a token cannot present a
`(issuer, subject)` it owns with a `presence` it does not, and thereby key into another
principal's idempotency slot. The identity tuple is validated as a unit in D1, not trusted
field-by-field.

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

### Exact lifecycle sequences (hit / miss-success / miss-error / cancel) — Yua required

```
IDEMPOTENT WRITE — HIT:
  authn-mw(principal) → authorize(403?) → idempotent_replay: lookup HIT
    → raise Replay(cached) → exception handler → 2xx + X-Idempotent-Replay:true
  → store-mw: idem_ctx set? yes, but replay=true → DO NOT re-store; release nothing (no lock acquired on a hit)

IDEMPOTENT WRITE — MISS, SUCCESS:
  authn-mw → authorize → idempotent_replay: lookup MISS → ACQUIRE in-flight(identity_key)
    → request.state.idem_ctx = {identity_key, token} → handler → 2xx
  → store-mw: idem_ctx set, 2xx, non-streaming, non-replay → STORE(identity_key, response)
    → finally: RELEASE in-flight(token)

IDEMPOTENT WRITE — MISS, HANDLER ERROR (4xx/5xx):
  ... → ACQUIRE → handler raises/returns 5xx (as a RESPONSE)
  → store-mw: idem_ctx set but status not 2xx → DO NOT store
    → finally: RELEASE in-flight(token)   (nothing cached; next attempt re-executes)

CANCEL / DISCONNECT before response:
  ... → ACQUIRE → client disconnects mid-handler
  → store-mw finally (runs on the cancellation/exception path): RELEASE in-flight(token)
    (no stuck marker; no cache)

CONCURRENT MISS (same identity, Phase 0 single-worker):
  req1: ACQUIRE ok → handler running
  req2: ACQUIRE fails (in-flight held) → 409/425 (or bounded wait for req1's result)
```

The store middleware's **release is in `finally`** on all four paths; **cache write is
guarded by 2xx + non-streaming + non-replay**. Proven against a live app.

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
