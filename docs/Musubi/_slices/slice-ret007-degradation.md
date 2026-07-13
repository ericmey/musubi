---
title: "Slice: RET-007 — retrieval degradation propagation (C5+H11+M15)"
slice_id: slice-ret007-degradation
section: _slices
type: slice
status: done
owner: aoi
phase: "Retrieval-audit 2026-07-13 — degradation propagation red contract (Yua-authorized)"
tags: [section/slices, status/done, type/slice, retrieval, degradation, observability]
updated: 2026-07-13
reviewed: true
depends-on: []
blocks: [slice-ret007-degradation-impl]
issue: 416
---

# Slice: RET-007 — retrieval degradation propagation (C5+H11+M15)

**Red contract — DOCS + TESTS ONLY, no `src/musubi/**`.** Transcribes Shiori's accepted contract
(harem-ops `e9ef562`, `projects/active/hermes-musubi-provider/briefs/c5-h11-m15-degradation-contract.md`)
into a rerunnable Musubi red contract. The fix (surface bounded warnings + total-failure errors up
through core → router → schema → SDK → MCP → LiveKit → metrics) is a SEPARATE implementation slice.
**This red contract was ACCEPTED by Yua on 2026-07-13 at 870de16** (6 pass + 11 strict-xfail,
tc-coverage 17/17, two-slice boundary accepted); the implementation slice is authorized to begin
from it. Tracking Issue #416.

## The defect (contract §1)
The retrieval stack conflates infrastructure failure with an empty result set: Qdrant/sparse timeouts
and reranker crashes are caught internally and mapped to `200 OK` empty/degraded results; the router
strips the internal `warnings` array; every adapter extracts `res["results"]` and discards the rest —
so the agent is blind to degradation.

## Two-slice topology (Yua-accepted)
- **THIS slice (Musubi repo):** core / router / schema / SDK / MCP / LiveKit / metrics.
- **SEPARATE dependent slice (Hermes):** the Hermes provider lives in the `hermes-agent` repo (NOT
  Musubi), tracked via harem-ops `hermes-musubi-provider`; Issue #417. It DEPENDS-ON this slice's HTTP
  warnings contract and exposes a `warnings: [codes]` array in its JSON tool response.
  `test_hermes_provider_surfaces_warnings` lives THERE, not here — the boundary is not crossed.

## Contract invariants encoded
- Allowlisted warning codes ONLY (§4): `sparse_embedding_failed`, `reranker_failed`,
  `plane_timeout_<plane>`, `plane_error_<plane>` — machine-readable, never free-text.
- Healthy no-match → `200 OK` with `warnings == []` (§2). Total failure → `503 BACKEND_UNAVAILABLE`
  or `500 INTERNAL` explicit error envelope, never a `200` empty.
- Additive schema (§3): `RetrieveResponse.warnings: list[str] = []`.
- Two NEW bounded Prometheus metrics (§6): `musubi_retrieval_warnings_total`,
  `musubi_retrieval_errors_total`.

## Contract conflicts surfaced (not graded on a curve)
- **A (format):** `retrieve/blended.py` already emits FREE-TEXT warnings (`"plane X failed: ..."`);
  the contract requires allowlisted codes — the fix must convert. Encoded by
  `test_partial_plane_failure_surfaces_warning`.
- **B (healthy-zero semantics):** `blended.py` appends `"no hits in any plane"` on a healthy empty
  result, contradicting §2. Encoded by the distinct red `test_healthy_zero_match_has_no_warning`.

## Owned paths
`owns_paths` (tests + this doc only):
- `tests/retrieve/test_ret007_degradation.py` (core: 4 controls + 6 reds)
- `tests/api/test_ret007_http_warnings.py` (wire-shape + telemetry reds)
- `tests/sdk/test_ret007_sdk_warnings.py` (2 SDK passthrough controls)
- `tests/adapters/test_ret007_adapter_warnings.py` (MCP + LiveKit reds)
- `docs/Musubi/_slices/slice-ret007-degradation.md` (this file)

`forbidden_paths`:
- `src/musubi/**` — NO src in the red contract; the fix is the separate implementation slice.
- the `hermes-agent` repo — separate dependent slice (do not cross).

## Specs to implement

- [[_slices/slice-ret007-degradation]] — a red-contract slice; its contract IS the `## Test Contract`
  below (transcribed from Shiori's accepted brief). At this head the reds are strict-xfail (each
  reason names its RET-007 contract defect) and the controls pass, so `make tc-coverage
  SLICE=slice-ret007-degradation` exits 0 with no missing bullet.

## Test Contract

6 PASS controls + 11 strict-XFAIL reds = 17 (each red fails for its named contract reason today).

Controls (ordinary PASS):
1. `test_control_healthy_zero_match` legitimate zero-match → empty Ok.
2. `test_control_successful_rerank` healthy reranker scores candidates.
3. `test_control_successful_sparse` healthy sparse embedding returns results.
4. `test_control_successful_blended` healthy blended returns hits, no warnings.
5. `test_sync_sdk_preserves_warnings` sync SDK passes the raw warnings array (already transparent).
6. `test_async_sdk_preserves_warnings` async SDK passes the raw warnings array (already transparent).

Reds (strict-xfail; flip to PASS in the fix commit):
7. `test_c5_hybrid_timeout` C5 — hybrid timeout must be Err, not Ok([]).
8. `test_h11_blended_all_plane_failure` H11 — all-plane failure must be an Err envelope, not Ok(empty).
9. `test_m15_sparse_timeout_silent_fallback` M15 — sparse timeout must surface `sparse_embedding_failed`.
10. `test_m15_rerank_failure_silent_fallback` M15 — reranker failure must surface `reranker_failed`.
11. `test_partial_plane_failure_surfaces_warning` partial failure → bounded `plane_*` code (Conflict A).
12. `test_healthy_zero_match_has_no_warning` healthy zero-match → warnings==[] (Conflict B).
13. `test_http_wire_shape_drops_warnings` router must not strip `warnings` at the HTTP boundary.
14. `test_telemetry_bounded_labels` the two bounded metrics must exist (§6).
15. `test_mcp_adapter_surfaces_warnings` MCP must prepend the fixed-prefix degradation note.
16. `test_livekit_fast_talker_surfaces_warnings` FastTalker must surface warnings to ChatContext.
17. `test_livekit_slow_thinker_surfaces_warnings` SlowThinker must surface warnings to ChatContext.

**Test Contract Closure state: ✓ satisfied at red-contract head** — `make tc-coverage
SLICE=slice-ret007-degradation` exits 0: controls PASS, reds `⏭ skipped` (strict-xfail, each reason
names the RET-007 contract defect). No missing bullet, no final-stack illusion.

## Status

**`done`** (2026-07-13) — red contract ACCEPTED by Yua at 870de16 (6 PASS + 11 strict-XFAIL,
tc-coverage 17/17, zero src, two-slice boundary accepted). `done` is the pre-merge closure state (PR
#419 + Issue #416 open until merge). The implementation slice (separate) is authorized from here; the
Hermes provider is a separate dependent slice (Issue #417). Not merged/deployed.

spec-update: slice-ret007-degradation — NEW red contract for C5/H11/M15 degradation propagation;
two-slice topology (Musubi + separate Hermes); Conflicts A (free-text warnings) + B (healthy-zero
warning) surfaced (Yua 2026-07-13).
