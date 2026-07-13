---
title: "Slice: Phase B — routed post-authz idempotency pipeline (SEC-002 + IDEM-001)"
slice_id: slice-idempotency-phase-b
section: _slices
type: slice
status: in-progress
owner: aoi
phase: "Security audit 2026-07-12/13 — narrow source, Phase B (Yua-authorized 2026-07-13T00:17)"
tags: [section/slices, status/in-progress, type/slice, security, p0, auth, idempotency]
updated: 2026-07-13
reviewed: false
depends-on: [slice-auth-boundary-red-contract, slice-sec-002-idempotency-auth-bypass]
blocks: []
---

# Slice: Phase B — routed post-authz idempotency pipeline (SEC-002 + IDEM-001)

**P0 · SEC-002 + IDEM-001.** Narrow source authorization from Yua (REQ 2026-07-13T00:17), stacked
on the **frozen** Phase A branch `slice/auth-boundary-phase-a` / PR #403 @ `a1c916e`. Replaces the
pre-auth idempotency middleware (the SEC-002 bug) with the routed **post-authz** pipeline proven by
the accepted D3 design spikes.

## Specs to implement

- [[_slices/slice-idempotency-phase-b]] — this remediation's contract is defined by its own
  `## Test Contract` below (the SEC-002 / IDEM-001 / REQ-5 red-contract), not a separate product
  spec; the slice is the canonical home for the closure matrix. Related security findings:
  [[_slices/slice-sec-002-idempotency-auth-bypass]], [[_slices/slice-auth-boundary-red-contract]].

## Evidence (do not orphan)

- Accepted D3 design contract: `slice/auth-boundary-design-spikes` @ **239029a** (kept on origin
  through final freeze, reference-only, no PR). D5 is **Phase C**, zero D5 src here.
- Accepted D5 design (Phase C): same branch, `tests/api/spikes/test_d5_multipart_digest_ingress.py`.

## Scope (authorized)

SEC-002 + IDEM-001 and the routed post-authz idempotency pipeline for **registered JSON write
routes only**. Implement:
- validated principal tuple `(issuer, subject, presence)` from the AuthContext;
- identity = principal tuple + method + `operation_id` + authorized namespace + Idempotency-Key;
- canonical body digest = exact received bytes + content-type (byte-exact, no semantic JSON
  equivalence); persisted separately from the identity;
- duplicate Idempotency-Key header → **400**; same identity + different digest → **409**; same
  identity + same digest → **replay**;
- dependency-edge authz **before** lookup; in-flight **lease** with release on all exits;
- **store-only pure-ASGI** response observer preserving raw headers/body, caching only a clean
  terminal 2xx **miss**.
- Single-worker fail-closed stays Phase A (unchanged here).

## Owned paths

`owns_paths` (src):
- `src/musubi/api/idempotency.py` — rewrite: lease acquire/release, principal-bound identity,
  digest-aware store/lookup (409/replay), duplicate-key handling.
- `src/musubi/api/idempotency_observer.py` — **NEW**: store-only pure-ASGI response observer.
- `src/musubi/api/idempotency_dependency.py` — **NEW**: routed post-authz idempotency dependency
  (+ route eligibility registration).
- `src/musubi/api/write_auth.py` — **NEW**: shared `AuthorizedWrite` dependency types.
- `src/musubi/api/routers/writes_episodic.py`, `writes_curated.py` — the body-derived capture
  routes only: the `AuthorizedWrite` edge + idempotency dependency; drop in-handler
  `_check_body_scope`. **Not Phase A files.** (`writes_concept.py` is query-derived and OUT of
  scope — see the named follow-up.)

`owns_paths` (tests):
- `tests/api/test_idempotency_contract.py` — **NEW**: transcribed D3 identity/order contracts +
  closure matrix (first production commit).
- FLIPS (inherited red-contract, not Phase A files): `tests/api/test_sec002_idempotency_auth.py`,
  `tests/api/test_idem001_replay_and_race.py`, `tests/api/spikes/test_idem_lease_contract.py`.

## Named app-composition wiring (Phase A file — unavoidable, explicitly named)

`src/musubi/api/app.py`: (a) REMOVE the idempotency lookup + store blocks inside `_wrapped_call`
(keeping the rate-limit logic they are interleaved with); (b) mount the store-only observer as a
pure-ASGI middleware; (c) wire the idempotency dependency onto the registered write routes. This
is the ONLY Phase A file touched, and only for composition wiring. `settings.py`/`deploy/systemd`
Phase A REQ-10 changes are NOT touched.

## FORBIDDEN (Phase B)

Multipart ingress cap / D5, artifact digest changes, Phase A files except the named app.py
wiring, retrieval/lifecycle/adapters, durable cross-process cache.

## APPROVED SPEC DRIFT — Option A (Yua REQ 2026-07-13T00:27)

Spec-update: idempotent JSON captures use an **explicit body-derived authorization dependency
edge**, not in-handler `_check_body_scope`. Yua chose (A) and rejected (B) — (B) double-
authenticates and lets idempotency interpret body security independently, a second path that can
drift.

Design (proven expressible in FastAPI with a SINGLE body parse — `parse_count == 1`):
1. A shared `AuthorizedWrite` context: `auth: AuthContext` + authorized `namespace` + the parsed
   `body` instance.
2. A per-capture body-auth dependency declares `body: <Model> = Body(...)` (the ONE parse), calls
   `authorize_namespace(request, body.namespace, settings, access="w")`, and returns
   `AuthorizedWrite`.
3. The idempotency dependency **explicitly `Depends`** on `AuthorizedWrite` (edge, no sibling
   order).
4. Handlers `Depends` the idempotency result, consume the SAME `ctx.body` (no re-declared body
   param), **remove `_check_body_scope`** (no duplicate defense-in-depth auth), and mutate the
   plane ONLY after the dependency chain.
5. `created_at` operator guard still reads `request.state.auth` (set by `authenticate_request` in
   the authz dependency) — red-locked.

Inventory (proven, not assumed): body-derived eligible captures are exactly `POST /v1/episodic`,
`POST /v1/episodic/batch`, `POST /v1/curated`. Concept routes are **query-derived** (`namespace:
str = Query(...)` + route-level `require_auth`) and are OUT of the body-drift scope (mutations on
existing objects, not captures).

`owns_paths` expanded (narrow): + `src/musubi/api/routers/writes_episodic.py`,
`writes_curated.py`, and a shared dependency-types module (`src/musubi/api/write_auth.py`).

spec-update: slice-idempotency-phase-b — Option A body-auth dependency edge (Yua 2026-07-13T00:27).

## Named follow-up: query-derived / concept mutations (Yua 2026-07-13T00:30)

Concept `reinforce`/`promote`/`reject` and other query-derived write mutations are **explicitly
out of Phase B scope** — recorded here as a NAMED follow-up decision, not a silent omission. They
are mutations on existing objects (not JSON captures), authorize a query-derived namespace via
route-level `require_auth`, and would integrate through a simpler `Depends(require_auth)` edge (no
body-derived parse). Whether they become idempotent-eligible is a **Phase B-follow-up inventory
decision** to be made explicitly.

**Tracking home:** to be filed as a dedicated GitHub Issue ("follow-up: query-derived idempotent
eligibility inventory") and linked here on creation; this named prose is the temporary placeholder
until that issue exists (per Yua 2026-07-13T00:36).

## Additional required reds (Yua 2026-07-13T00:30)

- OpenAPI `requestBody` schema + requiredness for all 3 routes stays byte/structure-equivalent in
  the material fields after the body moves into the dependency (proven feasible: FastAPI flattens
  a dependency `Body(...)` into the route's requestBody identically). Malformed-body 422 envelope
  stays compatible with the current contract.
- The handler cannot be invoked without the `AuthorizedWrite` / idempotency context through the
  normal route graph (route `dependant` structurally enforces the edge; no bypass path).

## Review blockers B1/B2/B3 + spec update (Yua 2026-07-13T01:48)

Post-`b5ad26c` source review found two source-level blockers and one broken test instrument;
fixed tests-first, each red demonstrated failing against `b5ad26c` before the fix.

- **B1 — observer buffering by eligibility, not candidacy.** The observer must NOT decide to buffer
  a response from method + `Idempotency-Key` header: an ineligible route (no idempotency dependency,
  e.g. the `POST /v1/retrieve/stream` `StreamingResponse`) carrying a key would then have its entire
  stream buffered — an unbounded-memory DoS (reproduced: 32 MiB stream → ~32 MiB retained). Fixed:
  buffer ONLY when the routed dependency has published the acquired-lease state, checked at
  `http.response.start` (the dependency runs strictly before the handler's response, so the state is
  present by then). Ineligible/streaming responses flow through with zero retention. Red measures
  non-retention via `tracemalloc` peak.

- **B2 — no time-based reclaim of a live lease (SPEC CHANGE).** The lease cache previously reclaimed
  any in-flight lease after `stale_after_s=30s`, framed as "crash recovery." That framing was wrong:
  this cache is **process-local and single-worker**, so a process crash destroys the whole cache —
  a time-based reclaim can never recover crash state and only lets a legitimately SLOW live request
  be re-executed into a **duplicate mutation**. Removed the time-based reclaim and the
  `stale_after_s` knob entirely. **New invariant:** a live in-flight lease is freed ONLY by its
  owner's request exit — `store` (success) or `release` (error/cancel); a hung owner fails closed
  (the key 409s until the process restarts), which is strictly safer than a double write. Only
  COMPLETED entries expire (after `ttl_s`). **Durable, cross-process crash recovery remains a named
  FUTURE store concern** (`slice-api-v0-write-distributed-idempotency`) — not this in-memory
  primitive. Contract-suite property `p6` inverted (in-flight never reclaimed by elapsed time);
  `p10` retargeted to prove clock-injection via the COMPLETED-lease TTL.

- **B3 — concurrency test now counts the mutation.** The real-route concurrency test asserted a
  single distinct `object_id`, which the content-addressed `EpisodicPlane` satisfies even if the
  mutation ran N times. Replaced with a synchronized counting `EpisodicPlane` spy that holds the
  winner's `create` in-flight (via Events) while 11 identical retries race the held lease; asserts
  `plane.create` call count == 1 and all retries are visible in_flight 409s. Red-proven by bypassing
  the acquire gate (every retry reaches `create`).

spec-update: slice-idempotency-phase-b — live leases fail closed, freed only by owned request exit;
no time-based reclaim; durable cross-process crash recovery is a named future store concern (Yua
2026-07-13T01:48, T02:09).

## Test Contract

Every bullet maps to a passing test (verified mechanically by `make tc-coverage
SLICE=slice-idempotency-phase-b`). Grouped by the contract they close.

SEC-002 — replay must never bypass authentication:
1. `test_no_bearer_must_not_replay` no bearer + known key + same body → 401, never a cached 2xx.
2. `test_invalid_bearer_must_not_replay` invalid bearer → 401, never a replay.
3. `test_cross_tenant_must_not_replay` a different tenant → 403, no disclosure of A's write.
4. `test_owner_can_replay_its_own_write` the authenticated original subject still replays.

IDEM-001 — identity binds the operation; the race is closed by an in-flight lease:
5. `test_same_key_body_must_not_replay_across_endpoints` same key+body on a different route ≠ replay.
6. `test_replay_on_same_endpoint_still_works` same key+body on the same route still replays.
7. `test_in_flight_lease_closes_the_race_second_caller_is_not_a_free_miss` the lease closes the race.
8. `test_conflict_still_detected_same_key_different_body` same key + different body → conflict.

Pipeline behaviour on the real capture routes:
9. `test_owner_replay_is_byte_exact_same_object_id` replay serves the exact cached bytes.
10. `test_real_route_concurrency_mutation_runs_exactly_once` 12 concurrent identical requests → `create` runs once.
11. `test_no_replay_across_routes` identity binds the route.
12. `test_no_replay_across_principals` identity binds the principal.
13. `test_no_replay_across_namespaces` identity binds the authorized namespace.

Store-only observer contract (release on every exit; no DoS; faithful store):
14. `test_observer_does_not_buffer_ineligible_stream` an ineligible stream + key is NOT buffered (B1).
15. `test_observer_stores_on_clean_2xx_then_replayable` clean 2xx stores a replayable entry.
16. `test_observer_releases_on_non_2xx` a non-2xx releases the lease.
17. `test_observer_store_failure_after_send_is_swallowed_and_retry_reexecutes` post-send store failure is swallowed (req 5).

Lease cache invariants:
18. `test_in_flight_lease_is_never_reclaimed_by_elapsed_time` a live lease is never reclaimed by time (B2).
19. `test_digest_conflict_without_lease_leak` a digest conflict acquires no lease.
20. `test_bounded_eviction_keeps_newest_completed` the completed set is bounded, newest kept.
21. `test_idempotency_key_expires_after_ttl` a completed entry expires at the TTL boundary.

Option A body-auth dependency edge:
22. `test_every_eligible_capture_route_carries_idempotency_dependency` all 3 captures carry the edge.
23. `test_handler_unreachable_without_authorized_write` no handler is reachable without the edge.

REQ-5 — faithful replay (raw headers, exact bytes, media type):
24. `test_replay_preserves_set_cookie` replay preserves Set-Cookie.
25. `test_replay_preserves_exact_body_bytes` replay preserves exact bytes.
26. `test_non_json_response_is_idempotent_and_keeps_media` non-JSON 2xx is cached with its media type.

**Test Contract Closure state: ✓ satisfied** — `make tc-coverage SLICE=slice-idempotency-phase-b`
reports 26 bullets, 26 ✓ passing, 0 missing (exit 0). No skipped/xfail bullets, no named deferrals:
every contract case is closed by a live passing test. (The two related security-finding slices
linked above carry prose checklists, not `test_`-named bullets, so they contribute 0 tracked
bullets — this slice is the closure home.)

## Verification gate

First commit = transcribed contract tests + closure matrix. Then implementation commits.
Targeted + full CI, coverage on owned files, `agent-check`, Yua review before merge. Remote CI:
temporarily retarget → main for exact-SHA evidence, then restore base to
`slice/auth-boundary-phase-a` (the pattern approved for #403). No merge until independent
acceptance + green. Merge order: #402 → main, retarget/recheck #403 → main, then Phase B → main.

## Status
Design drift resolved (Option A approved, Yua 2026-07-13T00:27); feasibility gates + inventory
proven; required reds landed (106f19c). Proceeding with the authorized Phase B src implementation
in reviewable commits (AuthorizedWrite edge → idempotency dependency → lease/digest cache →
store-only observer → named app.py wiring), flipping the SEC-002 / IDEM-001 / lease reds.
