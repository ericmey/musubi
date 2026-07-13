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

# Phase B — routed post-authz idempotency pipeline  ·  P0 (SEC-002 + IDEM-001)

Narrow source authorization from Yua (REQ 2026-07-13T00:17), stacked on the **frozen** Phase A
branch `slice/auth-boundary-phase-a` / PR #403 @ `a1c916e`. Replaces the pre-auth idempotency
middleware (the SEC-002 bug) with the routed **post-authz** pipeline proven by the accepted D3
design spikes.

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
