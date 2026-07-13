---
title: "Slice: auth-boundary Phase A — SEC-003 + SEC-004 + REQ-10"
slice_id: slice-auth-boundary-phase-a
section: _slices
type: slice
status: done
owner: aoi
phase: "Security audit 2026-07-12/13 — Phase A src (Yua-authorized REQ 2026-07-12T22:51)"
tags: [section/slices, status/done, type/slice, security, p0, auth]
updated: 2026-07-13
reviewed: true
depends-on: [slice-auth-boundary-red-contract, slice-sec-003-namespace-outside-query, slice-sec-004-contradictions-fleet-scroll]
blocks: []
issue: 410
---

# Slice: auth-boundary Phase A — SEC-003 + SEC-004 + REQ-10

The **canonical Phase A src slice** — the slice that authorizes the `src/musubi/**` changes PR #403
makes. It exists as a **docs-only spec repair** (Yua stack rulings 2026-07-13T02:48): the
`slice-auth-boundary-red-contract` slice explicitly `forbidden_paths: src/musubi/**`, so #403's
source changes had NO owning slice. This slice is that owner. Stacked on the red contract (#402) +
the accepted ADR (`ADR-auth-boundary-consolidation`).

Closed by **PR #403**. Tracking Issue #410. `status: done` is the pre-merge closure state the Vault
check requires when a PR `Closes` a slice's Issue — the PR and Issue are still OPEN, NOT merged.
**Production remains vulnerable to SEC-003/004 until this stack is merged AND deployed.**

## Specs to implement

- [[_slices/slice-auth-boundary-phase-a]] — this Phase A src slice's contract is its `## Test
  Contract` below; the ADR (`ADR-auth-boundary-consolidation`, D2 + D4 REQ-10) is the design. At
  #403 head every bullet is a passing test, so `make tc-coverage SLICE=slice-auth-boundary-phase-a`
  exits 0.

## Owned paths (src authorized here)

`owns_paths` (src — exactly PR #403's diff):
- `src/musubi/api/auth.py` — route-native `authorize_namespace` (D2): Form/Path/body-field namespace
  scope checks.
- `src/musubi/api/app.py` — **ONLY the REQ-10 single-worker fail-closed guard** (rejects
  `WEB_CONCURRENCY > 1`). **The Phase B idempotency observer/dependency composition wiring is NOT
  owned here** — that is `slice-idempotency-phase-b` (#404). This slice touches app.py for the
  REQ-10 guard and nothing else.
- `src/musubi/api/routers/contradictions.py` — SEC-004: omitted-namespace fanout requires operator;
  backend failure → 503, not empty 200.
- `src/musubi/api/routers/namespaces.py` — SEC-003: Path namespace on `namespace_stats`.
- `src/musubi/api/routers/writes_artifact.py` — SEC-003: Form namespace on `upload_artifact`.
- `src/musubi/settings.py` — REQ-10: `api_workers` (`le=1`) + `web_concurrency` read.
- `deploy/systemd/musubi-api.service` — REQ-10: `--workers 1` pin.
- tests: `tests/api/test_sec003_namespace_scope.py`, `test_sec004_contradictions_scope.py`,
  `test_req10_single_worker_fail_closed.py`, `test_api_v0_read.py` (adapted).

`forbidden_paths`:
- `src/musubi/api/idempotency*.py`, `idempotency_observer.py`, `idempotency_dependency.py`,
  `write_auth.py`, the capture-route idempotency wiring — **Phase B** (`slice-idempotency-phase-b`).
- multipart ingress-cap / D5 — Phase C.
- `src/musubi/retrieve/**`, `src/musubi/lifecycle/**`, adapters.

## Test Contract

Every bullet is a passing test at #403 head (`make tc-coverage SLICE=slice-auth-boundary-phase-a`
→ 0 missing). The matching SEC-003/004 reds on the red-contract slice (#402) flip green here.

SEC-003 — namespace outside the query string:
1. `test_upload_cross_tenant_namespace_must_be_403` Form namespace scope enforced.
2. `test_namespace_stats_cross_tenant_must_be_403` Path namespace scope enforced.
3. `test_upload_no_token_must_be_401` no-token control.

SEC-004 — contradictions fanout:
4. `test_ordinary_token_omitted_namespace_must_be_403` omitted namespace requires operator.
5. `test_backend_failure_must_not_be_empty_200` backend failure → 5xx, not empty 200.
6. `test_operator_omitted_namespace_succeeds_cross_namespace` operator fanout preserved.

REQ-10 — single-worker fail-closed:
7. `test_create_app_must_fail_closed_on_web_concurrency` create_app rejects WEB_CONCURRENCY>1.
8. `test_settings_must_reject_api_workers_gt_1` Settings.api_workers le=1.
9. `test_systemd_must_pin_single_worker` systemd pins --workers 1.

**Test Contract Closure state: ✓ satisfied at #403 head** — all 9 bullets passing, 0 missing.

## Status

**`done`** (2026-07-13) — Phase A src implemented and independently accepted; closed by PR #403.
`done` is the pre-merge closure state (Vault check: a PR that `Closes` a slice Issue requires the
slice to be `done`). PR #403 + Issue #410 are OPEN, NOT merged/deployed. Production stays vulnerable
until merge + deploy.

spec-update: slice-auth-boundary-phase-a — NEW canonical Phase A src slice (repairs the missing
owner for #403's src, which the red-contract slice forbids). App.py ownership scoped to the REQ-10
guard only; Phase B composition wiring excluded (Yua 2026-07-13T02:48 / T02:54 correction 7).
