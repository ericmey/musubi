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
- `src/musubi/api/routers/writes_episodic.py`, `writes_curated.py`, `writes_concept.py` — register
  eligibility + (pending drift resolution) authz-as-dependency-edge. **Not Phase A files.**

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

## OPEN DESIGN DRIFT — reported to Yua before coding (2026-07-13)

The D3 spike modeled auth as a clean route dependency (`authenticate` → `idem` edge). The real
**capture routes** (`POST /v1/episodic`, `/v1/curated`, batch) authorize the **body-derived**
namespace INSIDE the handler via `_check_body_scope`, NOT as a route dependency
(`writes_episodic.py:66-68` states scope is body-derived so it cannot use the query-reading
`require_auth` dependency). A route-level idempotency dependency would therefore run BEFORE that
in-handler authz → the SEC-002 pre-auth replay bug. Options (awaiting Yua's direction):
- (A) refactor the capture-route body-namespace authz into a body-reading dependency edge, then
  the idempotency dependency `Depends` on it;
- (B) the idempotency dependency itself performs the body-namespace authz (read body, authorize,
  then lookup) — self-contained; the handler's `_check_body_scope` becomes defense-in-depth.
Routes that already use route-level `require_auth` (PATCH/DELETE, concept POST) are unaffected.

## Verification gate

First commit = transcribed contract tests + closure matrix. Then implementation commits.
Targeted + full CI, coverage on owned files, `agent-check`, Yua review before merge. Remote CI:
temporarily retarget → main for exact-SHA evidence, then restore base to
`slice/auth-boundary-phase-a` (the pattern approved for #403). No merge until independent
acceptance + green. Merge order: #402 → main, retarget/recheck #403 → main, then Phase B → main.

## Status
Branch created; reporting design drift before coding the dependency.
