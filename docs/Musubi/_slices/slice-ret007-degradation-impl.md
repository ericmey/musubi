---
title: "Slice: RET-007 degradation propagation — IMPLEMENTATION (explicit envelope)"
slice_id: slice-ret007-degradation-impl
section: _slices
type: slice
status: in-review
owner: aoi
phase: "Retrieval-audit 2026-07-13 — RET-007 implementation (Yua-authorized, explicit-envelope design)"
tags: [section/slices, status/in-review, type/slice, retrieval, degradation, observability]
updated: 2026-07-13
reviewed: false
depends-on: [slice-ret007-degradation]
blocks: []
issue: 422
---

# Slice: RET-007 degradation propagation — IMPLEMENTATION (explicit envelope)

Implements the ACCEPTED red contract (`slice-ret007-degradation`, #416, merged f79d2b2). Tests-first:
this commit lands the EXPANDED red set; the source (the explicit-envelope refactor) follows in the
same slice, flipping every red to green. No merge/deploy without Yua's independent review.

## Locked design (Yua 2026-07-13)
- **Explicit typed success envelope** through the retrieve internals: `results` (rows) + a
  `tuple[RetrievalWarning, ...]`, where `RetrievalWarning` = a bounded `code` + an explicit FIXED
  `plane`. NOT a list subclass (slicing/sorting/casts silently lose metadata) and NOT a widened
  global `Ok`.
- **Metadata must survive** slicing / sorting / cross-plane fanout / dedup; warnings aggregate across
  targets with **no loss and no duplicate**, deduped to distinct `(code, plane)` ONLY at the final
  request boundary.
- **Unified bounded failure `kind`** propagated hybrid → deep → blended → orchestration (mapped from
  each layer's `code`, never inferred from free-text `detail`). The router's existing kind→status
  table is preserved EXACTLY: timeout→503, internal→500, **bad_query→400 (caller-caused, NOT
  relabelled 500)**, forbidden→403.
- **Status contract:** partial timeout + surviving hits → `Ok` envelope (200 + warnings); all-timeout
  / no hits → `Err(timeout)` → 503; any internal/bad_query per policy → bounded `Err`.
- **Telemetry** at the final orchestration boundary: `musubi_retrieval_warnings_total{warning,plane}`
  (once per distinct `(warning,plane)` per request) + `musubi_retrieval_errors_total{kind}` (once per
  failed request). Fixed plane set only.
- **Boundary drift removed:** the cross-plane fanout's `transient_any` BOOLEAN (which discarded WHICH
  plane timed out) is replaced by structured per-plane warnings preserved before the merge.

## Owned paths
`owns_paths` (this expanded-red commit — tests + this doc):
- `tests/retrieve/test_ret007_envelope.py` (NEW: metadata survival, multi-target aggregation/dedup,
  structured-bounded warning, direct deep degradation).
- `tests/api/test_ret007_status_and_telemetry.py` (NEW: kind→status controls incl. bad_query→400,
  per-request telemetry cardinality red).
- `docs/Musubi/_slices/slice-ret007-degradation-impl.md` (this file).

`forbidden_paths`: the `hermes-agent` repo — Hermes is a SEPARATE dependent slice (#417). Auth/idempotency
files (unrelated).

## Source-refactor paths (NOT claimed by this red commit — to be claimed at the src commit)

These are the paths the explicit-envelope source refactor will touch. They are listed here as
prose, NOT in `owns_paths`, because this commit is tests-only; the claim happens at the src commit.
**Two OVERLAP other slices' owns_paths — a coordination point flagged to Yua before source:**
- `src/musubi/retrieve/{hybrid,deep,blended,orchestration,rerank,fast}.py` — envelope + unified `kind`.
- `src/musubi/api/responses.py` — additive `RetrieveResponse.warnings: list[str] = []`.
- `src/musubi/api/routers/retrieve.py` — **OVERLAPS `slice-api-retrieve-wildcards`** — surface warnings.
- `src/musubi/observability/` — **OVERLAPS `slice-ops-observability`** — the two bounded metrics.
- `src/musubi/adapters/{mcp,livekit}/` — surface warnings (MCP note; LiveKit `last_warnings`).
- flips the strict-xfail decorators across the RET-007 red files in the same commit.

## Specs to implement

- [[_slices/slice-ret007-degradation-impl]] — this implementation slice's contract is its
  `## Test Contract` below (the expanded envelope reds). At this head the reds are strict-xfail
  (each reason names the envelope defect) and the control passes, so `make tc-coverage
  SLICE=slice-ret007-degradation-impl` exits 0. Design is the accepted contract [[_slices/slice-ret007-degradation]].

## Test Contract (expanded)

Inherited from the accepted red contract (#416, in main): 6 controls + 11 strict-xfail reds (SEC/M15/
HTTP/adapters/SDK). This slice ADDS:

Controls (green now, must stay green — guard the status semantics):
1. `test_total_failure_status_mapping_control` timeout→503 / internal→500 / bad_query→400 / forbidden→403.

Reds (strict-xfail; flip to PASS with the envelope):
2. `test_multi_target_aggregates_warnings_no_loss` per-plane timeout survives the cross-plane merge.
3. `test_multi_target_dedupes_warnings_per_request` distinct `(code,plane)` deduped to one.
4. `test_envelope_warnings_survive_slice` warning survives `[:limit]`.
5. `test_partial_failure_warning_is_structured_and_bounded` structured `RetrievalWarning(code, fixed-plane)`.
6. `test_direct_deep_degradation_surfaces_warning` direct deep path surfaces `sparse_embedding_failed`.
7. `test_telemetry_per_request_cardinality` metric counts once per distinct `(warning,plane)`.

**Closure at this head:** full RET-007 set = 10 passed + 17 xfailed (inherited 6+11 plus this slice's
4+6). tc-coverage/ruff/mypy/check.py clean; zero src in this red commit.

## Status

**`in-review`** (2026-07-13) — expanded red set landed (tests-first, zero src). The explicit-envelope
source refactor is authorized to follow in this slice after the red commit (Yua 2026-07-13). Tracking
Issue #422; depends-on the accepted red contract (#416); Hermes is a separate dependent slice (#417).

spec-update: slice-ret007-degradation-impl — NEW implementation slice for RET-007; explicit typed
success envelope (results + tuple[RetrievalWarning{code,plane}]); metadata survives fanout/dedup/
sort/slice; unified bounded `kind`; bounded {warning,plane}/{kind} telemetry; removes the
`transient_any` boolean metadata-loss seam (Yua 2026-07-13).
