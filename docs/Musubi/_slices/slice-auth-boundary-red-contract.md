---
title: "Slice: auth-boundary red-contract — SEC-002/003/004 + IDEM-001 consolidated"
slice_id: slice-auth-boundary-red-contract
section: _slices
type: slice
status: done
owner: aoi
phase: "Security audit 2026-07-12/13 (Eric, discoverer) — Yua router/reviewer"
tags: [section/slices, status/done, type/slice, security, p0, auth, idempotency]
updated: 2026-07-13
reviewed: true
depends-on: [slice-sec-002-idempotency-auth-bypass, slice-sec-003-namespace-outside-query, slice-sec-004-contradictions-fleet-scroll]
blocks: []
issue: 406
---

# Slice: auth-boundary red-contract — SEC-002/003/004 + IDEM-001 consolidated

Consolidates the three security reds (SEC-002/003/004) with the new **IDEM-001** race +
cross-endpoint-replay reds and the FastAPI 0.136 split-pipeline spike, into **one rerunnable
contract**. This is the "first commit" Yua authorized (REQ 2026-07-12T21:18): **test-only,
no `src/musubi/**`.** Tracking: GitHub Issue #406; closed by **PR #402** (this red contract + ADR).

## Specs to implement

- [[_slices/slice-auth-boundary-red-contract]] — this is a red-contract slice; its contract IS the
  `## Test Contract` below. At THIS PR's head the security reds are strict-xfail (documenting the
  holes) and each xfail reason names its closing slice; the spike + controls pass. So
  `make tc-coverage SLICE=slice-auth-boundary-red-contract` exits 0 at #402 head with no missing
  bullet — no final-stack-only illusion.

## Test Contract

Numbered bullets resolve at THIS PR's (#402) head: reds are `⏭ skipped` (strict-xfail, reason names
the closing slice); controls + spike are `✓ passing`.

SEC-002 (closing slice: slice-idempotency-phase-b / #404):
1. `test_no_bearer_must_not_replay` no-bearer replay must 401 (xfail here).
2. `test_invalid_bearer_must_not_replay` invalid-bearer replay must 401 (xfail here).
3. `test_cross_tenant_must_not_replay` cross-tenant replay must 403, no disclosure (xfail here).
4. `test_owner_can_replay_its_own_write` owner replay preserved (control, passing).

SEC-003 (closing slice: slice-auth-boundary-phase-a / #403):
5. `test_upload_cross_tenant_namespace_must_be_403` Form namespace scope (xfail here).
6. `test_namespace_stats_cross_tenant_must_be_403` Path namespace scope (xfail here).
7. `test_upload_no_token_must_be_401` no-token control (passing).

SEC-004 (closing slice: slice-auth-boundary-phase-a / #403):
8. `test_ordinary_token_omitted_namespace_must_be_403` omitted-namespace fanout (xfail here).
9. `test_backend_failure_must_not_be_empty_200` backend failure → 5xx (xfail here).
10. `test_operator_omitted_namespace_succeeds_cross_namespace` operator fanout control (passing).

IDEM-001 (closing slice: slice-idempotency-phase-b / #404):
11. `test_same_key_body_must_not_replay_across_endpoints` cross-endpoint replay (xfail here).
12. `test_second_concurrent_caller_must_not_get_free_miss` in-flight lease race (xfail here).
13. `test_replay_on_same_endpoint_still_works` same-endpoint replay control (passing).

FastAPI split-pipeline spike (mechanics proven, passing):
14. `test_middleware_sees_dependency_state_after_call_next` dependency-edge topology.
15. `test_typed_replay_becomes_a_response` typed Replay → response.
16. `test_store_gate_is_status_based_not_try_except` status-based store gate.
17. `test_500_is_a_response_not_an_exception` 500 is a response, not an exception.

**Test Contract Closure state: ✓ satisfied at #402 head** — `make tc-coverage
SLICE=slice-auth-boundary-red-contract` → 0 missing; reds skipped with closing-slice reasons, spike +
controls passing.

## Scope

`owns_paths`:
- `tests/api/test_idem001_replay_and_race.py`   (NEW)
- `docs/Musubi/_slices/slice-auth-boundary-red-contract.md`  (this file)
- (existing, unchanged in behaviour) `tests/api/test_sec002_idempotency_auth.py`,
  `test_sec003_namespace_scope.py`, `test_sec004_contradictions_scope.py`,
  `sec003_route_inventory.py`, `tests/api/spikes/test_split_pipeline_spike.py`

`forbidden_paths`:
- `src/musubi/**` — the auth boundary is frozen; the fix is ADR-gated
  (`ADR-auth-boundary-consolidation`).

## Runnable command

```
uv run pytest tests/api/test_sec002_idempotency_auth.py \
              tests/api/test_sec003_namespace_scope.py \
              tests/api/test_sec004_contradictions_scope.py \
              tests/api/test_idem001_replay_and_race.py \
              tests/api/spikes/test_split_pipeline_spike.py -v
```

`xfail(strict=True)` throughout: a red FAILS today (documents the hole) and turns
XPASS→fail the moment the fix lands — an unexpected pass signals the fix, never a broken test.

## Observed evidence (not inferred)

IDEM-001(A), cross-endpoint replay, directly observed through the real app:

```
A  POST /v1/episodic        -> 202  X-Idempotent-Replay=None   object_id=3GQcH5dUS7TmPw3wcdpdB10AbjK
B  POST /v1/episodic/batch  -> 202  X-Idempotent-Replay=true   object_id=3GQcH5dUS7TmPw3wcdpdB10AbjK
```

A single-`CaptureResponse` (`object_id`+`state`+`dedup`) was replayed onto the **batch**
endpoint — a different `operation_id` — with the replay header set. The middleware
(`app.py:_wrapped_call`) looks up `cache.lookup(idem_key, body)` with no route/operation in
the identity, **before** the route validates.

IDEM-001(B), the race, proven deterministically at the cache unit level: two `lookup(key,body)`
calls before either `store` BOTH return `"miss"` — there is no acquire/lease primitive.

## Mapping — Yua's ten requirements → rerunnable contracts

Honest status per requirement. `SPIKE-PROVEN` = green mechanics proof committed;
`RED-XFAIL` = strict red committed, fails today; `TO-WRITE` = named contract, not yet
encoded; `UNPROVEN` = design exists, no prototype (blocks the matching `src` fix).

| # | Requirement (Yua) | Encoded by | Status |
| --- | --- | --- | --- |
| 1 | Commit the FastAPI 0.136 spike/tests (not prose) | `spikes/test_split_pipeline_spike.py` (6/6) | **SPIKE-PROVEN** |
| 2 | authz is a dependency EDGE, not sibling declaration order | `test_middleware_sees_dependency_state_after_call_next` (topology: the dep runs inside `call_next`, its state is visible to the outer store) | SPIKE-PARTIAL — the explicit "gates regardless of declaration order" ordering test is **TO-WRITE** |
| 3 | release on 2xx, returned/raised 4xx/5xx, cancellation, cache-store failure, response-send failure; store failure must not fake a cached success | `test_store_gate_is_status_based_not_try_except`, `test_500_is_a_response_not_an_exception` (2xx + 5xx release proven) | PARTIAL — cancellation / store-failure / send-failure paths **TO-WRITE** |
| 4 | no 3xx/4xx/5xx, replay, or true stream cached; define streaming detection under BaseHTTPMiddleware | `test_middleware_distinguishes_hit_from_miss`, `test_typed_replay_becomes_a_response`, `test_store_gate_is_status_based_not_try_except`; streaming: `test_streaming_detection_is_unsettled_...` | reds/gates SPIKE-PROVEN; **streaming detector UNSETTLED** (isinstance disproven, body_iterator disproven, Content-Length is a heuristic — real design is a route cacheability contract / ASGI `more_body` observer, ADR D3) |
| 5 | preserve raw duplicate headers, cookies, media type, background task, exact bytes — no lossy reconstruction | — | **TO-WRITE** (adversarial: duplicate Set-Cookie, non-JSON media, background task, byte-exactness) |
| 6 | multipart digest: file bytes + canonical small fields, per-route size, rewind/spool, no collision | — | **UNPROVEN** — ADR D5, no prototype; blocks the D5 `src` fix |
| 7 | D6 exact token invariant; reject inconsistent issuer/subject/presence | partial via `test_sec002_*` (replay under wrong bearer) | **TO-WRITE** — explicit issuer/subject/presence-mismatch rejection |
| 8 | public absent-bearer stays public; presented-invalid fails; protected absent fails | `test_*_no_token_must_be_401` (protected-absent) across sec002/003/004 | PARTIAL — the public-route absent-vs-invalid pair is **TO-WRITE** |
| 9 | identity includes route/operation — same key+body cannot replay across endpoints | `test_same_key_body_must_not_replay_across_endpoints` (+ `test_replay_on_same_endpoint_still_works` control) | **RED-XFAIL** (new, observed above) |
| 10 | Phase-0 single-worker invariant fails closed in runtime config, not only a unit assertion | — | **TO-WRITE** — a config-level fail-closed check, not just a unit assert |

**Four of ten are commitable red/green contracts now (1, 4-partial, 9, plus the SEC reds).**
The rest are named, tracked contracts the eventual `src` fix must satisfy — enumerated here so
none hides behind prose. The two hard blockers before any `src` fix: **streaming detection
(req 4)** and **the multipart digest (req 6, ADR D5)** are both explicitly UNPROVEN.

## Core invariant (Yua, carried)

Every write's replay identity must bind the caller's authorization AND the endpoint/operation
(D6 + req 9), the pipeline must never turn a completed mutation into an ambiguous cached
success (req 3), and streaming/non-2xx responses must never be cached (req 4). Reading identity
as `(key, body)` alone is the defect; the fix binds `(key, body, route, authorized-identity)`
behind an in-flight lease.

## Status

**`done`** (2026-07-13) — red contract landed and accepted; `src` was authorized by the router and the fix
implemented + accepted downstream (Phase A #403, Phase B #404). The reds here stay strict-xfail at
#402 head (each reason now names its closing slice); they flip green at their closing PR's head.
Closed by PR #402. Tracking Issue #406.
