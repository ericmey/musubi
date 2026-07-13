---
title: "Slice: RET-007 degradation propagation — IMPLEMENTATION (explicit envelope)"
slice_id: slice-ret007-degradation-impl
section: _slices
type: slice
status: done
owner: aoi
phase: "Retrieval-audit 2026-07-13 — RET-007 implementation (Yua-authorized, explicit-envelope design)"
tags: [section/slices, status/done, type/slice, retrieval, degradation, observability]
updated: 2026-07-13
reviewed: true
depends-on: [slice-ret007-degradation]
blocks: []
issue: 422
---

# Slice: RET-007 degradation propagation — IMPLEMENTATION (explicit envelope)

Implemented the accepted red contract (`slice-ret007-degradation`, #416, merged `f79d2b2`).
PR #424 merged the independently accepted implementation to `main` at `e474780`; Issue #422 is
closed. Deployment and live fault-injection proof remain tracked outside this completed source slice.

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
`owns_paths` (tests + this doc):
- `tests/retrieve/test_ret007_envelope.py` (NEW: metadata survival, multi-target aggregation/dedup,
  structured-bounded warning, direct deep degradation).
- `tests/api/test_ret007_status_and_telemetry.py` (NEW: kind→status controls incl. bad_query→400,
  per-request telemetry cardinality red).
- `tests/retrieve/{test_hybrid,test_deep,test_blended,test_fast}.py` — **contract migration** (Yua
  ruling 2026-07-13): repoint each old assertion that encodes pre-envelope behavior (hybrid Ok([])→Err
  on timeout; deep/blended/fast free-text → bounded RetrievalWarning codes; deep list → envelope) onto
  the accepted contract. NOT weakening — every non-RET007 behavior each test still guards is kept.
  *(Note: `test_hybrid.py` ownership was subsequently transferred to [[_slices/slice-ret004-evals]] / Issue 430 on 2026-07-13).*
- `tests/api/test_context.py` + `tests/cli/test_cli_context.py` — the /v1/context degradation red
  (this commit) then its migration at src.
- `tests/retrieve/test_ret007_kind_propagation.py` + `tests/api/test_ret007_telemetry_boundary.py` +
  `tests/adapters/test_ret007_livekit_channel.py` (NEW — Yua's five blockers, second round).
- `tests/retrieve/test_rerank.py` (DRIFT-4 — rerank shape migration).
  *(Note: `tests/retrieve/test_rerank.py` ownership was subsequently transferred to [[_slices/slice-ret004-evals]] / Issue 430 on 2026-07-13).*
- `docs/Musubi/_slices/slice-ret007-degradation-impl.md` (this file).

`owns_paths` (the source-refactor — SAME slice, claimed NOW so ownership is explicit before source
per Yua 2026-07-13; these files are written in the src commit that follows this red commit):
- `src/musubi/retrieve/{hybrid,deep,blended,orchestration,rerank,fast}.py` — the explicit envelope +
  unified `kind` + bounded per-plane warnings.
- `src/musubi/api/responses.py` — additive `RetrieveResponse.warnings: list[str] = []` (wire schema).
- `src/musubi/api/routers/retrieve.py` — surface `warnings` on the Ok path; total failure → Err→status.
- `src/musubi/observability/` — the two bounded metrics.
- `src/musubi/adapters/{mcp,livekit}/` — surface warnings (MCP fixed-prefix note; LiveKit
  `last_warnings`).
- `src/musubi/api/routers/context.py` + `src/musubi/retrieve/context_pack.py` + `src/musubi/cli/context.py`
  — **/v1/context degradation surfacing** (Yua ruling 2026-07-13): the canonical context surface must
  not return degraded context indistinguishable from healthy. `ContextPack.warnings` additive
  (default-empty); router threads the bounded codes off the envelope; CLI `_render` visibly renders
  them on the non-JSON path (JSON preserves them naturally).
- flips the strict-xfail decorators across the RET-007 red files in the same commit.

**Fast ruling (Yua 2026-07-13):** `run_fast_retrieve` MUST emit the same bounded `RetrievalWarning`
codes as every other mode — ONE warning language, no translation seam. `test_fast.py` migrates with
the rest.

`forbidden_paths`: the `hermes-agent` repo — Hermes is a SEPARATE dependent slice (#417). Auth/idempotency
files (unrelated).

## Overlap resolution — both overlapping slices are DONE (not live contention)

The mechanical `owns_paths` check flags two files this slice claims as also claimed elsewhere. Both
owning slices are **`status: done`** (merged) — verified in-repo, not assumed — so the overlap is
*historical* ownership of already-shipped code, not two live branches racing the same file:

- **`src/musubi/api/routers/retrieve.py` ↔ `slice-api-retrieve-wildcards` (`status: done`).** That
  slice owns this file for the *wildcard namespace expansion* logic. RET-007's change is orthogonal
  and purely additive: it surfaces the degradation `warnings` on the already-built Ok path and maps a
  total-failure `Err(kind)` to the existing status table — it does not touch expansion. No live
  contention: the wildcard slice shipped; this is a later additive extension of the same file.
- **`src/musubi/observability/` ↔ `slice-ops-observability` (`status: done`).** That slice *created*
  the observability module (registry, exposition, middleware). RET-007 *extends* it with two bounded
  counters (`musubi_retrieval_warnings_total{warning,plane}`, `musubi_retrieval_errors_total{kind}`) —
  the module's intended growth surface, not a rewrite. No live contention.

Because both owners are done, ownership here is unambiguous: this slice is the live owner of these
paths for the RET-007 additions. `check.py` still emits an advisory `⚠` for the path appearing in
two slices (it does not special-case done slices) — surfaced here rather than suppressed; it is
advisory (exit 0), not an error, and does not modify the shared checker.

## Yua implementation rulings (2026-07-13 — DRIFT-1/2/3 resolved)

- **DRIFT-1 → Option C.** Keep global `Ok` pure. `hybrid_search` returns `Ok[HybridSearchResult]`
  (warnings on `.value`, consistent with orchestration's `Ok[RetrievalEnvelope]`). The accepted
  `test_ret007_degradation.py` is added to owns_paths and its two access lines migrate: M15 →
  `result.value.warnings`, healthy control → `result.value.hits == []` (access migration, contract
  preserved; red-proof it).
- **DRIFT-2 → frozen structured `RetrievalWarning(code, plane)`, NOT a str subclass.** The inherited
  string-allowlist assertion (`_is_allowlisted`) is the STALE contract — migrate it to read
  `.code`/`.plane` and red-proof it. No metadata smuggled into `str`.
- **DRIFT-3 → approved.** The four inherited red files join owns_paths SOLELY to flip their exact
  xfails after the defect is green.
- **`src/musubi/retrieve/warnings.py` authorized** as the type home (avoids a `hybrid.py` knot).
- Invariants to preserve: zero-warning healthy shape; stable ordering/dedup; bounded telemetry labels.

**DRIFT-4 (found during impl — reported in the SHA receipt, applying the DRIFT-1 precedent):** the
accepted `test_m15_rerank_failure_silent_fallback` red reads `.warnings` directly off `rerank()`'s
return, while the accepted `test_control_successful_rerank` indexes it as a list — the SAME
shape/pattern DRIFT-1 resolved for hybrid. Applied the identical resolution: `rerank()` returns a
frozen `RerankResult(hits, warnings)` carrying `reranker_failed` on the fallback path; migrated the
control to `.hits` and the red to `.warnings` (red-proofed). `tests/retrieve/test_rerank.py` (the only
downstream test of `rerank.py`) was NOT in the recorded owns_paths — added it, migrated its
list-access to `.hits`. Also added `src/musubi/observability/retrieval_metrics.py` (the two bounded
counters, under the owned `observability/`) and applied defensive router-side `dedupe()` so the
per-request cardinality holds regardless of caller path.

Added to `owns_paths` per the ruling: `src/musubi/retrieve/warnings.py` (NEW type home);
`tests/retrieve/test_ret007_degradation.py`, `tests/api/test_ret007_http_warnings.py`,
`tests/adapters/test_ret007_adapter_warnings.py`, `tests/sdk/test_ret007_sdk_warnings.py` (flip
xfails / migrate the stale allowlist + access assertions only).

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
7. Telemetry per-request cardinality → moved to the shared-boundary contract below (Blocker 5): the
   counting lives at `orchestration.retrieve`, so the test exercises real orchestration (mocking
   `_run_single`), not the router in isolation — see `test_ret007_telemetry_boundary.py`.

Second-round contract (Yua's five blockers, 2026-07-13 — tests-first, red-proofed):
- Blocker 1+2 (`test_ret007_kind_propagation.py`): a real hybrid `qdrant_timeout` through deep/fast and
  a blended all-timeout reach `kind=timeout`; a non-timeout total failure stays `internal`; an
  all-fast-timeout is `Err(503)`, not `Ok(empty)`. (Not just the router table — real pipeline faults.)
- Blocker 3+5 (`test_ret007_telemetry_boundary.py`): telemetry counted once at the shared
  orchestration boundary (so `/v1/context` + direct orchestration count too; `/v1/retrieve` doesn't
  double-count); boundedness fails closed — a non-allowlisted code/plane or a code/plane mismatch is
  dropped before the wire or a Prometheus label.
- Blocker 4 (`test_ret007_livekit_channel.py`): the slow→cache→fast path preserves degradation status
  (the cache carries warnings); a total failure is visible via `LiveKitAdapter.retrieval_status` (the
  consumer); no stale warning survives a healthy/error transition.

/v1/context degradation reds (tests-only, landed BEFORE the context src per Yua; strict-xfail):
8. `test_context_degraded_response_carries_warnings` (test_context.py) — a degraded retrieve makes the
   /v1/context wire response carry the bounded warning codes (not indistinguishable from healthy).
9. `test_context_nonjson_renders_warnings` (test_cli_context.py) — the non-JSON CLI visibly renders the
   degradation codes.
Controls (green now + post-impl): `test_context_healthy_response_default_empty_warnings` (healthy →
warnings default-empty, additive); `test_context_json_preserves_warnings` (JSON path naturally
preserves them).

**Closure at this head:** full RET-007 set = 12 passed + 19 xfailed (inherited #416 6+11 plus this
slice's 6+8 — envelope/telemetry 4+6 and /v1/context 2+2). tc-coverage/ruff/mypy/check.py clean; zero
src in the red commits.

## Status

**`in-review`** (2026-07-13) — the explicit-envelope SOURCE has landed and every RET-007 red flipped
green (the reds and their access migrations are red-proofed). Full test suite green, mypy strict clean,
ruff clean. Awaiting Yua's independent review of the source SHA before merge — no merge/deploy without
it. Tracking Issue #422; depends-on the accepted red contract (#416); Hermes is a separate dependent
slice (#417).

spec-update: slice-ret007-degradation-impl — NEW implementation slice for RET-007; explicit typed
success envelope (results + tuple[RetrievalWarning{code,plane}]); metadata survives fanout/dedup/
sort/slice; unified bounded `kind`; bounded {warning,plane}/{kind} telemetry; removes the
`transient_any` boolean metadata-loss seam (Yua 2026-07-13).
