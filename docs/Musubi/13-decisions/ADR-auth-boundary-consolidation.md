---
title: "ADR: consolidated auth boundary — SEC-002/003/004 + IDEM-001"
section: 13-decisions
type: adr
status: accepted
owner: aoi
discoverer: eric
reviewed_by: yua
phase: "Security audit 2026-07-12/13"
tags: [type/adr, status/accepted, security, auth, idempotency]
updated: 2026-07-13
supersedes: []
---

# ADR: consolidated auth boundary — SEC-002/003/004 + IDEM-001

**Discoverer of all four defects: Eric.** Source-confirmed and routed by Yua. Design: Aoi.
**Status: ACCEPTED (rev 4).** The decision is accepted. The implementation is **accepted-for-merge,
NOT shipped**: it lives in the UNMERGED stacked PRs #403 (Phase A) and #404 (Phase B, independently
accepted). **It is NOT merged to `main` and NOT deployed — production remains vulnerable to
SEC-002/003/004 + IDEM-001 until this stack lands.** Every "accepted" claim below describes the
accepted stack state, not production.

## Rev 4 — reconciled to the accepted-for-merge implementation (PR #403 + accepted PR #404)

Rev 1–3 were the pre-implementation design. **Git history preserves Rev 1–3 in full; Rev 4 REWRITES
the superseded Decision sections (D1–D6, sequences, matrix) IN PLACE** — the Rev1–3 rev-note blocks
remain below, but their Decision bodies are replaced, not kept verbatim. Rev 4 corrects the doc to
the code that was actually implemented and independently accepted in the unmerged stack, SEPARATES
the accepted subset from the still-deferred work, and removes the "no src until approved" gating —
src was narrowly authorized by the router (Yua) and Phase B accepted at `fafca2c`. The design shape
DID change during implementation and this Rev records the shape that landed in the PRs (unmerged),
not the Rev3 sketch:

- **Accepted subset:** D1 (authn co-located in the authz dependency), D2 (route-native authz on the
  named Form/Path/body-field routes + contradictions fanout), D3 (routed AuthorizedWrite auth edge +
  route-declared idempotency eligibility + pure-ASGI store-only observer), D4 Phase 0 (in-process
  lease, single-worker fail-closed, **no time-based reclaim of a live lease**), D6 (identity tuple).
- **Deferred, exact:** D4 Phase 1 (durable cross-process); D5 (Phase C — design PROVEN @239029a,
  implementation not started); REQ7 (identity tuple-consistency validation — xfail); REQ8 (public
  absent-vs-invalid bearer — xfail).

Reminder throughout: "accepted" = the accepted stack state (PR #403/#404, unmerged), NOT production.

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

### D1 — AUTHENTICATION co-located in the route-native authz dependency (accepted PR #403/#404)

> **Rev 4 (accepted):** the Rev1–3 "one canonical early authn middleware setting
> `request.state.principal`" was NOT built. Authentication co-locates with authorization in the
> route dependency, which removes the pre-auth surface entirely (the SEC-002 root cause) rather than
> adding an early layer.

Authentication is performed by `authenticate_request` INSIDE the route-native authorization
dependency — not a standalone early ASGI middleware. For a valid, unexpired, correctly-signed bearer
it yields an `AuthContext(issuer, subject, presence, scopes, audience, token_id)` attached to
`request.state.auth`; an absent/invalid bearer on a **protected** route (one carrying the auth
dependency) → 401. There is no separate early-authn layer and no `request.state.principal` dict —
the validated principal tuple IS the AuthContext. **Routes that do not carry the protected auth
dependency are unchanged by this ADR** (their auth posture is out of scope here; note the repo has no
`/health` route today, and a metrics-exposure policy is a separate open finding — neither is claimed
public by this ADR). SEC-002's "replay before auth" cannot occur because there is no
route-independent replay step (see D3).

### D2 — route-native AUTHORIZATION from PARSED values

Authorization runs as a **shared route dependency** that receives the **already-parsed**
namespace from its real source, via per-source helpers:

```
authorize(access, namespace_source)   where namespace_source ∈
  { from_query, from_path, from_body_field, from_form_field }
```

Omitted/nullable namespace is an **explicit fanout contract**: `require_operator`, or a
named authorized-fanout dependency — never an implicit all-tenant scroll.

> **Rev 4 (accepted, scoped):** what the accepted stack implements is the named, covered surface —
> the Form namespace on `upload_artifact`, the Path namespace on `namespace_stats`, the body-field
> namespaces on the three JSON captures (via D3's AuthorizedWrite edge), and the omitted-namespace
> fanout on `GET /v1/contradictions` (operator-only, backend failure → 5xx not empty 200). This does
> NOT claim generic query auth was deleted, nor that every namespace-bearing route was converted.
> `tests/api/sec003_route_inventory.py` is a **regression guard for the covered surface** — it holds
> that line against a new route silently forgetting authz on the surfaces in scope; it is not a proof
> of universal enforcement across the whole API.

### D3 — routed AuthorizedWrite auth edge + route-declared eligibility + pure-ASGI store-only observer (accepted PR #404)

> **Rev 4 (accepted):** the Rev1–3 "thin store middleware after `call_next`" shape was superseded.
> A `BaseHTTPMiddleware`/`call_next` store is REJECTED — it collapses the response to a lossy
> `_StreamingResponse`. The accepted store is a **pure-ASGI store-only observer**.

The security decision is a routed dependency; the store is a pure-ASGI store-only observer. As
accepted:

- **AuthorizedWrite auth edge (Option A).** Each body-derived JSON capture (`POST /v1/episodic`,
  `/v1/episodic/batch`, `/v1/curated`) has a body-auth dependency that parses the body ONCE,
  authorizes the body-derived namespace, and returns `AuthorizedWrite(auth, namespace, body)`. The
  idempotency dependency EXPLICITLY `Depends` on it, so authn + namespace authz run before any cache
  lookup. In-handler `_check_body_scope` removed. **Hit** → `raise Replay(cached)` (typed; the
  registered handler serves the byte-exact cached response with `X-Idempotent-Replay: true` added on
  a fresh header list — the cached tuple is never mutated; the handler is not executed). **Miss** →
  the dependency acquires the in-flight lease and publishes `request.state.idem` (a frozen
  `IdempotencyRequestState`) + the exact lease cache, established BEFORE the response starts.
- **Route-declared idempotency eligibility.** ONLY routes carrying the idempotency dependency are
  cache-eligible; a route-inventory test guards that the three captures carry the edge and that no
  handler is reachable without it. This is the cacheability contract.
- **Pure-ASGI store-only observer (mounted outermost).** At `http.response.start` it reads whether
  the request published a lease; ONLY THEN does it buffer, storing a clean terminal 2xx as an
  immutable `CompletedResponse` (exact status / raw-headers / bytes). A **successful store RETAINS**
  that completed entry as the replay cache and does NOT release. It performs no lookup, no authz, no
  body read. The `finally` releases only a **NON-STORED** acquired lease — non-2xx, handler
  exception, send failure, cancellation, and a store that itself raises (a post-send store failure is
  swallowed + metered `musubi_idempotency_store_failures_total`, never fakes a cached success; the
  lease is released so the retry re-executes).

**Cacheability is SETTLED for the registered non-streaming JSON captures — and only for them.** The
observer does NOT detect or classify streams and makes no `isinstance`/`Content-Length` inference.
The mechanism is negative and contractual: the eligible routes are the three JSON captures, which
are contractually non-streaming and inventory-guarded; every other route — including the
`StreamingResponse` on `POST /v1/retrieve/stream` — publishes NO lease, so the observer NEVER
buffers it (it streams straight through with zero retention). So this closes the Rev3 "streaming
UNSETTLED" **for the registered surface**; it makes NO claim that streaming is universally settled,
only that ineligible responses are never touched.

**Why the store gate is status-based (proven):** `HTTPException(500)` becomes a **500 response**,
not an exception, so a try/except gate would cache the 500. The observer stores only on a clean
terminal 2xx for an acquired lease; a successful store retains the entry, and the `finally` releases
only the non-stored paths.

Because D3 runs only after D1+D2, an unauthenticated or cross-tenant request is refused (401/403) and
**never reaches lookup**. Cache identity (D6) is defense-in-depth on top.

### D4 — IDEM-001 concurrency, PHASED (Phase 0 accepted PR #404; Phase 1 DEFERRED)

Qdrant is not a lock/CAS store. Phase it:

- **Phase 0 (accepted PR #404):** secure authn/authz/replay (D1–D3, D6) **plus in-process atomic
  lease ownership** of the miss→execute→store window, under a single-worker deployment fail-closed
  and CONFIG-TESTED at three layers (REQ-10, accepted PR #403): `Settings.api_workers` (`le=1`),
  `create_app` rejects `WEB_CONCURRENCY > 1`, systemd `--workers 1`. A concurrent same-identity miss
  gets a visible `409` (in_flight) — never a double-execute.
  > **Rev 4 correction vs Rev1–3:** a LIVE in-flight lease is **NEVER reclaimed by elapsed time.**
  > Any stale/timeout "crash recovery" reclaim is wrong: the cache is process-local, so a crash
  > destroys the WHOLE cache — a time-based reclaim can never recover crash state and only
  > re-executes a slow-but-live request into a DUPLICATE mutation. An in-flight entry may be
  > transitioned ONLY by its owner: `store` atomically COMPLETES it and RETAINS the entry as the
  > replay cache (it does NOT free/delete it), while `release` REMOVES an incomplete entry on a
  > non-stored exit (error / cancel / non-2xx / a store that itself fails). A hung owner fails closed
  > (409 until the process restarts). Only COMPLETED entries expire (TTL).
- **Phase 1 (DEFERRED — distinct future store/ADR):** durable **cross-process** ownership for
  multi-worker (`slice-api-v0-write-distributed-idempotency`). Not implemented; multi-worker
  idempotency stays unimplemented and fail-closed by REQ-10.

### D5 — bound the body materialization (design PROVEN @239029a; implementation Phase C, DEFERRED)

> **Rev 4 (design proven, impl deferred):** Rev3's "UNPROVEN" is superseded — the design was proven
> in a runnable spike at `slice/auth-boundary-design-spikes` @ **239029a**
> (`tests/api/spikes/test_d5_multipart_digest_ingress.py`). The IMPLEMENTATION is **Phase C** and is
> NOT in Phase A/B (explicitly forbidden in the Phase B scope). Two distinct concerns, do not
> conflate them:

**(1) DoS control = a pure-ASGI ingress cap (the primary control).** A pure-ASGI layer counts the
**actual total encoded bytes** of the request as they arrive **before any parsing**, and rejects at
the per-route ceiling: exact-max **passes**, max+1 → **413**. It **distrusts `Content-Length`** (a
header is not a measurement) and counts the **real multipart framing bytes** (boundaries, part
headers), not just field/file payloads. Starlette's `max_part_size` separately bounds non-file
fields; **files spool/roll to disk, they are not rejected** by the part cap — so the ingress byte cap
is what actually bounds memory/disk for large uploads. Per-route (an upload route and a small JSON
write have different legitimate ceilings).

**(2) Identity/fidelity = a streamed digest (NOT the DoS control).** Distinct from the cap, the
canonical idempotency digest must distinguish two different files without holding the whole upload in
RAM: a **domain-separated**, **length-prefixed** hash of the small structured fields combined with a
**streamed SHA-256 of the file part**, **order-independent** (part order cannot be a security
convention — FastAPI parses/spools independently, so the digest must not depend on `namespace`
arriving before `file`). The digest+rewind/spool is for identity and replay fidelity, not for
bounding ingress. Two different uploads with identical form fields cannot collide on one key.

### D6 — identity is issuer + subject + presence (NOT jti)

Yua's correction: **`jti` is per-token and rotates; it is not rotation-stable.** The cache
identity is `(issuer, subject, presence)` so a legitimate token refresh for the same
principal still replays its own write. `jti` may be recorded in structured logs for audit
but is **not** part of the cache key. Chosen explicitly.

**Validate the tuple's internal consistency** (Yua): `issuer`, `subject`, and `presence`
must be mutually consistent per the token contract — e.g. `presence` must be the
subject's declared presence, not an arbitrary claim — so a token cannot present a
`(issuer, subject)` it owns with a `presence` it does not, and thereby key into another
principal's idempotency slot.

> **Rev 4 (accepted, REQ7 OPEN):** the identity tuple `(issuer, subject, presence)` + method +
> `operation_id` + authorized namespace + Idempotency-Key, with a byte-exact canonical digest
> (domain-sep + content-type + exact bytes) persisted separately, is IMPLEMENTED and accepted (PR
> #404). The tuple's internal-consistency VALIDATION is **NOT yet enforced**:
> `tests/api/test_req7_token_identity_invariant.py` remains strict-xfail — **REQ7 is OPEN**
> (deferred). (Related: REQ8, public absent-vs-invalid bearer, `test_req8_*` — also open/xfail.)

This also resolves the rev-1 strategy comparison: "exact-token fingerprint" (A) is rejected
precisely because it keys on the rotating secret; **(B) principal (issuer+subject+presence)
+ re-authorize** is adopted; per-route replay (C) is rejected.

## Executable request sequences (post-fix)

> **Rev 4:** authn co-locates in the authz dependency (D1), so the leading `authz-dep` step is
> `authenticate_request` + namespace authz together; the store is the pure-ASGI observer (D3), not a
> `store-only observer`. Multipart is Phase C (design proven @239029a, impl deferred).

```
JSON write (accepted PR #404):
  authz-dep(authn+authz from body-field "namespace","w")(401/403?)  [AuthorizedWrite edge]
    → Depends(idempotency)(hit→Replay | conflict/in_flight→409 | miss→acquire) → handler
    → [pure-ASGI observer stores clean 2xx for an acquired lease]

Multipart upload (Phase C — design proven @239029a, impl deferred):
  ingress-cap(413?) → authz-dep(from_form_field,"w")(401/403?) → Depends(idempotency)(streamed digest) → handler → observer

Path stats (GET, no idem — accepted PR #403):
  authz-dep(authn+authz from_path "namespace_path","r")(401/403?) → handler

Query read (unchanged behaviour — NOT converted by this ADR):
  route: Depends(require_auth(access="r")) → authenticate_request + query-"namespace" scope check(401/403?) → handler

Nullable fanout (contradictions — accepted PR #403):
  authz-dep(operator required if namespace omitted)(403 if ordinary+omitted) → scroll (filtered) | backend fail → 5xx
```

Every arrow is a real FastAPI dependency edge that runs in that order — no step consumes a
decision that has not yet been made.

### Exact lifecycle sequences (hit / miss-success / miss-error / cancel) — Yua required

```
IDEMPOTENT WRITE — HIT:
  authz-dep(authn+authz) → idempotency dependency: acquire returns HIT
    → raise Replay(cached) BEFORE publishing any lease state → handler exception handler → 2xx + X-Idempotent-Replay:true
  → store-only observer: NO request.state.idem was ever set (Replay raised first), so the observer
    sees no acquired lease → it does NOTHING: no buffer, no store, no release. The replay response
    passes straight through.

IDEMPOTENT WRITE — MISS, SUCCESS:
  authz-dep → idempotency dependency: acquire MISS → ACQUIRE lease
    → request.state.idem = IdempotencyRequestState(identity, owner, digest); request.state.idem_cache = <cache>
    → handler → 2xx
  → store-only observer: lease published, clean terminal 2xx → STORE the CompletedResponse (does NOT
    release — the completed entry IS the replay cache)

IDEMPOTENT WRITE — MISS, HANDLER ERROR (4xx/5xx):
  ... → ACQUIRE → handler raises/returns non-2xx (as a RESPONSE)
  → store-only observer: lease published but status not 2xx → DO NOT store
    → finally: RELEASE the lease   (nothing cached; next attempt re-executes)

CANCEL / DISCONNECT before response:
  ... → ACQUIRE → client disconnects mid-handler
  → store-only observer finally (runs on the cancellation/exception path): RELEASE the lease
    (no stuck marker; no cache)

CONCURRENT MISS (same identity, Phase 0 single-worker):
  req1: ACQUIRE ok → handler running
  req2: ACQUIRE returns in_flight (live lease held) → 409 (visible conflict; NO bounded wait, NO 425)
```

The store-only observer's **`finally` releases every NON-STORED acquired lease** (non-2xx, handler
exception, cancel/send failure, and a store that itself fails) — a **successful store atomically
completes and RETAINS the entry as the replay cache and does NOT release it** (and a HIT never
acquired a lease, so there is nothing to release). The store is guarded by a clean terminal 2xx for
an acquired lease (eligibility, not a stream heuristic). Accepted against a live app in PR #404.

## Error / status compatibility

401 unauth · 403 unauthorized namespace / ordinary+omitted fanout · cross-tenant replay 403
with **no cached body/object_id disclosed** · backend (Qdrant) failure **5xx, never empty
200** (folds SEC-004/RET-007) · owner replay **2xx + `X-Idempotent-Replay: true`** · concurrent
same-identity miss → first 2xx, second **409** (visible in_flight conflict; no bounded wait, no
425) · oversize body **413** (D5, Phase C). No response
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

Column = the ACCEPTED STACK state (PR #403/#404, unmerged), NOT production. "accepted" here means the
red flipped green at that PR's head; production stays vulnerable until merge.

| test | accepted-stack state | closed by |
| --- | --- | --- |
| no-bearer / invalid-bearer replay → 401 | accepted (#404) | D1 + D3 (no pre-auth replay path) |
| cross-tenant replay → 403 + no disclosure | accepted (#404) | D3 + D6 |
| owner replay → 2xx replay | accepted (#404) | D3 + D6 |
| key+body must not replay across endpoints (IDEM-001A) | accepted (#404) | D3 op-bound identity |
| upload cross-tenant Form ns → 403 (SEC-003) | accepted (#403) | D2 from_form_field |
| stats cross-tenant Path ns → 403 (SEC-003) | accepted (#403) | D2 from_path |
| own-ns upload/stats → 2xx; no-token → 401 | accepted (#403) | D1 + D2 |
| ordinary + omitted ns → 403 (SEC-004) | accepted (#403) | D2 fanout contract |
| backend failure → 5xx not empty 200 (SEC-004/RET-007) | accepted (#403) | error-handling |
| concurrent miss → no double-mutate (IDEM-001B) | accepted (#404) | D4 Phase 0 |
| faithful replay (headers/cookies/bytes/media, REQ-5) | accepted (#404) | D3 observer |
| ineligible stream + key not buffered (B1) | accepted (#404) | D3 route-declared eligibility |
| tuple issuer/subject/presence consistency (REQ-7) | XFAIL — OPEN | REQ7 (deferred) |
| public absent-vs-invalid bearer (REQ-8) | XFAIL — OPEN | REQ8 (deferred) |
| oversize multipart → 413 (D5) | deferred | Phase C (design proven @239029a) |

## Ownership / process (Yua's proposal, adopted)

- **Implementation owner:** Aoi — implemented and accepted-for-merge on `slice-auth-boundary-phase-a`
  (Phase A src, PR #403) and `slice-idempotency-phase-b` (Phase B src, PR #404, accepted), NOT this
  red-tests-only slice. (The Rev1–3 note that impl belongs on a separate slice is the reason the
  canonical `slice-auth-boundary-phase-a` was added in #403.)
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

Exact deferrals carried forward from the accepted stack:
- **D4 Phase 1** — durable cross-process idempotency (`slice-api-v0-write-distributed-idempotency`).
- **D5 Phase C** — multipart ingress-cap + streamed digest; design PROVEN @239029a, impl not started.
- **REQ7** — identity tuple internal-consistency validation (`test_req7_*`, strict-xfail).
- **REQ8** — public route absent-vs-invalid bearer (`test_req8_*`, strict-xfail).

Pre-existing (different root cause / different ADR): `_operator_scope_hint` rate-tier read; DQ-001
projection/summary; LIFE-007/008 / DATA-001 atomicity.

---

**Status: ACCEPTED (rev 4).** Implementation is accepted-for-merge in the unmerged stack (#403/#404),
NOT merged and NOT deployed — production remains vulnerable until the stack lands.
