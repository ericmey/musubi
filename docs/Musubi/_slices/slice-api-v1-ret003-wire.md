---
title: "Slice: RET-003 ranked vs recent wire contract"
slice_id: slice-api-v1-ret003-wire
section: _slices
type: slice
status: ready
owner: tama
phase: "slice-api-v1 — additive wire contract"
tags: [section/slices, status/ready, type/slice, area:api, area:retrieval, wire-contract, ranked-mode, recent-mode, additive]
updated: 2026-07-13
reviewed: false
depends-on: ["[[_slices/slice-retrieval-hybrid]]", "[[_slices/slice-retrieval-scoring]]"]
blocks: []
issue: 435
---

# Slice: RET-003 ranked vs recent wire contract

**Authoritative spec (DO NOT DUPLICATE):** the locked RET-003 wire contract is at
**harem-ops commit `e8c116c2`** (merging `d05e0a6`) on branch `chore/tama-ret003-spec` via **PR3**.
The implementation MUST obey the spec verbatim; tests-first; **zero `src/` in the first commit**;
follow-up commits land Pydantic / orchestration / snapshot changes in dependency order.

Spec file: `projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md`
on the harem-ops branch. Read it first.

**Status:** tests-first slice, `ready`, owner: Tama (Per Yua 2026-07-13 10:08:41, RET-003
tests-first lane is assigned to Tama. Aoi remains exclusively on C6. Shiori's RET-004 lane is in
flight and is NOT to be touched.)

## Why

The `/v1/retrieve` public wire is currently missing:
- top-level `state` (LifecycleState enum, 7 values) and `importance` (int 1..10) on every row,
- top-level `score_kind` discriminator per row mode,
- top-level `provenance_score` on recent rows (exact-table-only),
- 5-key typed `extra.score_components` for ranked (compat path; ranked mode currently emits 3),
- recent mode `extra.score_components = {}` typed empty (currently emits a fabricated 0/1/0).

The contract is locked; tests are the first commit; implementation follows.

## Spec source (must read first)

`projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md` on branch
`chore/tama-ret003-spec` at commit `d05e0a6a92a7c58ad2aae37232c82432a8ae95ec` (merged via PR3
at commit `e8c116c2bfe474263a53b27b882d5038fd0870b5`, merged at 2026-07-13). The 5 correction cycles
(Yua 2026-07-13 09:39:26, 09:49:53, 09:55:38, 10:00:42, 10:03:03) are all applied additively.

## Two-slice topology (this is the additive-API slice)

- **THIS slice (Musubi repo):** additive wire contract — the `src/musubi/api/responses.py` Pydantic
  models, the `src/musubi/api/routers/retrieve.py` router, the `src/musubi/retrieve/orchestration.py`
  carrier of source state/importance, the `src/musubi/retrieve/recent.py` recent branch, the
  `tests/api/test_api_v0_read.py` + `tests/api/test_retrieve_recent.py` consumers.

- **SEPARATE dependent slice (`slice-api-v1-ret003-runtime-snapshot`):** regeneration of the
  repo-root `openapi.yaml` from the runtime `/v1/openapi.json` + a new `slice-api-v*` ADR for the
  additive-API contract. Tracking: future Issue (deferred to a follow-up slice).

- **SEPARATE dependent slice (`slice-api-v1-ret003-orchestration`):** if any of the wire
  changes requires orchestration / scoring changes (`src/musubi/retrieve/scoring.py`,
  `src/musubi/retrieve/orchestration.py`), those land in a separate slice. Tracking: future Issue.

## What the spec requires (for the implementation)

1. **Top-level response variants** with `mode` discriminator (rows have NO `mode`).
   - `RankedRetrieveResponse(mode: Literal["fast", "deep", "blended"], results: list[RankedResultRow])`
   - `RecentRetrieveResponse(mode: Literal["recent"], results: list[RecentResultRow])`
2. **`extra` is TYPED**, not `dict[str, Any]`:
   - `RankedExtra(score_components: RankedScoreComponents, lineage: dict | None)`
   - `RecentExtra(score_components: RecentScoreComponents = {}, lineage: dict | None)` (exact `{}`, never `null`)
3. **`RankedScoreComponents`** has 5 fields, ALL REQUIRED with `Field(ge=0, le=1)`,
   `model_config = ConfigDict(extra='forbid')` — non-empty input fails loud (500).
4. **`RecentScoreComponents`** has no fields, `model_config = ConfigDict(extra='forbid')` — OpenAPI
   asserts `additionalProperties: false` (exact empty `{}`, never `null`).
5. **Top-level `state` and `importance`** on every row, BOTH NULLABLE; source-backed; never silently
   defaulted/clamped from internal Hit defaults.
6. **Top-level `score_kind`**: `"ranked_combined"` for ranked, `"created_epoch"` for recent.
7. **`provenance_score`** (recent mode only) is exact-table-only:
   `None` when state is missing OR `(plane, state)` is absent from `_PROVENANCE`; uses a new
   explicit `_provenance_score_for(plane, state) -> float | None` helper. Does NOT call
   `scoring._provenance` (which floors unknowns to 0.1).
8. **`RetrievalResult` carries `state` and `importance` BEFORE payload projection** because
   `hit.payload` can be `None` for `brief=true` in deep/blended. Orchestration owns source hits
   and constructs `RetrievalResult`. The router reads `hit.state` / `hit.importance` (always present,
   since orchestration populated them); the router does NOT read `hit.payload`.
9. **Corrupt source `state` / `importance`** (bad enum, out-of-range) → **500** (server integrity,
   NOT 422). Implementation must NOT clamp / coerce.
10. **API governance:** runtime Pydantic is the authoring truth; repo-root `openapi.yaml` is the
    committed deploy-time snapshot; docs skeleton `docs/Musubi/07-interfaces/openapi/musubi.v1.yaml`
    remains untouched in this slice.

## Owned paths (this slice; no `src/` in the first commit)

The first commit is tests-first; **zero `src/`**. The first commit only writes:
- `docs/Musubi/_slices/slice-api-v1-ret003-wire.md` — this file (slice contract)
- `docs/Musubi/_inbox/locks/slice-api-v1-ret003-wire.lock` — exclusive claim
- `docs/Musubi/_slices/adr-0013-additive-api-contract.md` (or appropriate next ADR number) — additive-API ADR
- `tests/api/test_retrieve_ret003_wire.py` (NEW, dedicated test file) — the 18 acceptance tests

The 18 tests live in a single new test file `tests/api/test_retrieve_ret003_wire.py` to keep
ownership clean. **None of the existing test files are modified** (the existing
`test_retrieve_result_carries_score_components_in_extra` at `tests/api/test_api_v0_read.py:869`
is updated to assert 5 keys, but that migration happens in a separate later commit when
`src/musubi/api/responses.py` is updated to add the new fields).

### `src/` paths owned (deferred to follow-up commits; not in this slice's first commit)

- `src/musubi/api/responses.py` — `RetrieveResultRow` (add typed `state`, `importance`, `score_kind`,
  typed `extra`); new `RankedRetrieveResponse` / `RecentRetrieveResponse` top-level variants
- `src/musubi/retrieve/orchestration.py:105-114` — `RetrievalResult` (add `state` and `importance`
  top-level fields); ranked branch populates `extra.score_components` with 5 keys using
  `reinforcement` (public name); recent branch populates `score_components` with exact `{}`,
  adds `score_kind="created_epoch"` and nullable `provenance_score`
- `src/musubi/retrieve/recent.py` — uses new `_provenance_score_for(plane, state) -> float | None`
  helper that returns `None` for missing state or absent `(plane, state)`
- `src/musubi/api/routers/retrieve.py:115-126` — reads `hit.state` / `hit.importance` (NOT
  `hit.payload`); propagates to `RetrieveResultRow`
- `root openapi.yaml` — regenerated from `/v1/openapi.json` (a separate snapshot PR)

## Slice ownership (the test paths the first commit creates / tests)

The 18 acceptance tests live in **one** new file: `tests/api/test_retrieve_ret003_wire.py`. They
are organized as:

- **§6.1 Ranked-mode (7 strict reds + 1 guard):** `test_retrieve_ranked_*` (8 tests)
- **§6.2 Recent-mode (5 strict reds):** `test_retrieve_recent_*` (5 tests)
- **§6.3 Source-truth vs internal-default (1 strict red):** `test_wire_importance_audits_internal_default`
- **§6.4 Runtime OpenAPI schema (2 strict reds):** `test_runtime_openapi_*` (2 tests)
- **§6.5 Regression guards (2):** `test_streaming_endpoint_excluded_from_this_contract_unchanged`,
  `test_extra_score_components_path_preserved_for_all_modes` (3 tests, but two are guards)
- **Brief preservation:** `test_retrieve_ranked_extra_score_components_has_five_keys` also asserts
  `brief=true` preserves top-level `state` / `importance` on a deep-mode row (per Yua 09:55:38 #6)
- **Total: 18 acceptance tests = 15 red + 3 guards**

## 19 acceptance tests (the contract; first-commit tests must match these names)

The count is 19 (not 18) because per Yua 2026-07-13 11:57:59 #2,
test #13 is split into two:

- `test_retrieve_recent_provenance_score_http_exact` (cases a, c via canonical recent HTTP)
- `test_retrieve_recent_provenance_score_seam_state_none` (case b via the
  orchestration->wire projection seam; canonical recent Qdrant never
  returns state=None because recent's state filter excludes it)

### Ranked-mode (7 strict reds + 1 guard)

1. `test_retrieve_ranked_top_level_state_present_required_nullable` — state key present, may be null
2. `test_retrieve_ranked_state_is_source_backed_not_fabricated` (valid + invalid) — 500 on bad enum
3. `test_retrieve_ranked_top_level_importance_present_required_nullable`
4. `test_retrieve_ranked_importance_is_source_backed_not_fabricated` (valid + invalid) — 500 on out-of-range
5. `test_retrieve_ranked_score_kind_is_ranked_combined`
6. `test_retrieve_ranked_extra_score_components_has_five_keys` — 5 keys (compat path); brief=true
7. `test_retrieve_ranked_score_is_combined_from_components` — test-local public-to-internal mapping
8. `test_retrieve_ranked_reinforcement_uses_full_word` (guard) — already passes current wire

### Recent-mode (5 strict reds)

9. `test_retrieve_recent_score_kind_is_created_epoch`
10. `test_retrieve_recent_extra_score_components_is_empty_dict_typed` — exact `{}`, never null
11. `test_retrieve_recent_top_level_state_present`
12. `test_retrieve_recent_top_level_importance_present` — seed state=provisional
    explicitly so the row traverses recent's state filter (Yua #1)
13a. `test_retrieve_recent_provenance_score_http_exact` (cases a, c) —
    episodic+matured=0.5; curated+provisional=None
13b. `test_retrieve_recent_provenance_score_seam_state_none` (case b) —
    orchestration->wire projection seam; state=None -> provenance_score=None

### Source-truth vs internal-default (1 strict red)

14. `test_wire_importance_audits_internal_default` — raw importance=null vs
    `score_components.importance=0.5`

### Runtime OpenAPI schema (2 strict reds)

15. `test_runtime_openapi_ranked_response_schema_required_with_five_components` —
    9 required row fields; required-nullable without defaults (Yua #3)
16. `test_runtime_openapi_recent_response_schema_required_with_empty_components` —
    10 required row fields; required-nullable without defaults (Yua #3)

### Schema discriminator + score-component exactness + typed-empty guard (3 strict reds)

17. `test_runtime_openapi_retrieve_response_is_oneof_mode_discriminated` (Yua #4) —
    POST /v1/retrieve 200 response is `oneOf` with a top-level `mode` discriminator
18. `test_retrieve_ranked_extra_score_components_exactly_five_and_values_in_unit_interval` (Yua #5) —
    exactly 5 keys, every value numeric in [0,1]
19. `test_recent_score_components_typed_empty_runtime_rejects_nonempty` (Yua #6) —
    runtime Pydantic rejects non-empty input at validation time

### Regression guards (2 — but they remain 2 green tests, not reds)

20. `test_streaming_endpoint_excluded_from_this_contract_unchanged` (guard)
21. `test_extra_score_components_path_preserved_for_all_modes` (guard)

(Numbers 20-21 are the re-anchored test positions; the file
currently collects 19 tests because the 2 guards are #18 and #19 in
the file and the 3 new strict reds are #17-#19 in the file. The
strict-reds + guards in the test file are 16 strict xfail + 3 pass
= 19 total; the spec table above lists 19 to keep the names
intact. The implementation slice changes the production source to
turn the 16 strict reds green; the 3 guards stay green before and
after.)

## Evidence (Yua #9)

The "every red is red-proofed against its named missing behavior"
claim is replaced with the honest evidence: `pytest
tests/api/test_retrieve_ret003_wire.py --runxfail` is the rerunnable
floor the tests-first slice commits to. It runs every xfail and
verifies each test fails at its NAMED assertion (not at setup or
row-selection). Implementation acceptance will require correct +
plausible-wrong mutation proof (i.e., the implementation slice
must also include bounded discrimination helpers — not in this
slice).

Per-test failure points from `--runxfail`:

- `test_retrieve_recent_top_level_importance_present`: fails at
  line 710, assertion `importance key missing` (the named red;
  row is present after the state=provisional fix).
- `test_retrieve_recent_provenance_score_http_exact`: fails at
  line 794, assertion `(a) provenance_score key missing` (the
  named red; row is present).
- `test_retrieve_recent_provenance_score_seam_state_none`: fails
  at line 844, ImportError on `_provenance_score_for` (the named
  missing seam).
- `test_runtime_openapi_retrieve_response_is_oneof_mode_discriminated`:
  fails at line 1118, assertion `oneOf with mode discriminator`
  missing (the named red).
- `test_retrieve_ranked_extra_score_components_exactly_five_and_values_in_unit_interval`:
  fails at line 1188, assertion `keys must be EXACTLY ...` (the
  named red).
- `test_recent_score_components_typed_empty_runtime_rejects_nonempty`:
  fails at line 1214, ImportError on `RecentScoreComponents` (the
  named missing model).

The remaining 10 strict reds (ranked 7, recent 4, source-truth 1,
openapi 2) similarly fail at their named assertions, not at setup
or row selection. This is the floor; the implementation acceptance
criterion is mutation proof, not just --runxfail.

## Status

slice — `ready`; tests-first; zero `src/` in the first commit. Implementation lands in a follow-up slice after the test contract lands. This slice is owned by Tama, not "awaits Aoi" (per Yua 2026-07-13 10:08:41 + 10:00:42).

## Depends on

- `docs/Musubi/_slices/slice-retrieval-hybrid.md` (status:done) — parent slice
- `docs/Musubi/_slices/slice-retrieval-scoring.md` (status:done) — parent slice

## Blocks

none (deferred to implementation slices)

## Owner

tama (Per Yua 2026-07-13 10:08:41, RET-003 tests-first lane is assigned to Tama. Aoi
remains exclusively on C6. Shiori's RET-004 lane is in flight and is NOT to be touched.)

## Hermes adapter follow-up (Yua 2026-07-13 10:56:23 closeout gate; corrected 11:19:50 + 11:57:59 #7)

Per Yua 2026-07-13 10:56:23, this slice is the natural seam for a SEPARATE follow-up: once the Musubi contract is stable, the Hermes adapter (`/Users/ericmey/Vaults/fleet-tools/hermes-plugins/musubi/__init__.py`, lines ~1200-1305) must preserve the following through without fabricating fields. Per Yua 2026-07-13 11:19:50 + 11:57:59 #7 correction, the current user plugin is a standalone Hermes user plugin (NOT core/MCP), and the current emitted shape is:

- The plugin emits Musubi's **logical API `object_id` (the stored KSUID)**, NOT the physical Qdrant point id. `episodic_point_id(object_id)` is a distinct UUID translation used internally to address the Qdrant point. The plugin's emitted `object_id` is the KSUID, e.g. `3GSGzQauqzXNPstBMJw3hcIV0yd`.
- The plugin discards `extra` entirely today; it does NOT already pass `score_components` through.
- `musubi_recall` is pinned to BLENDED ranked mode today; recent mode is NOT a current surface in the plugin.
- Recent passthrough is therefore only relevant if a future Hermes surface requests recent.

For the follow-up:

- **Ranked mode (the only current surface)**: the Hermes adapter must surface `state` (LifecycleState enum, 7 values, nullable for missing legacy) and `importance` (int 1..10, nullable for missing legacy) on the JSON row alongside the existing `object_id` (the KSUID, not the physical Qdrant point id); and must pass the 5-key `extra.score_components` dict (relevance, recency, importance, provenance, reinforcement) through without fabrication. The adapter must NOT fabricate values; it must null through for missing-legacy fields.
- **Recent mode**: only relevant if a future Hermes surface requests recent. When that lands, the adapter must surface `score_kind="created_epoch"` and `provenance_score` (nullable, exact-table-only).

This follow-up is a separate slice/branch (NOT this one). It is a "closeout gate" for the broader wire contract, secondary to Musubi correctness. This slice does not implement the Hermes adapter; the adapter lands in a follow-up that depends on this slice's wire contract.

## Out of scope

- Aoi C6 work (separate lane)
- Shiori's RET-004 (separate slice: `slice-ret004-evals`, Issue #430)
- `/v1/retrieve/stream` (RET-010 surface; out of scope for RET-003)
- `docs/Musubi/07-interfaces/openapi/musubi.v1.yaml` (stale skeleton; not hand-edited in this
  slice; regenerated as a separate `slice-api-v*` ADR step)
- SEC-005 binding-trace (separate lane)
- Nyla / Sumi consumer proof (separate lane)

## Spec source (harem-ops)

The locked RET-003 wire contract is at `projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md`
on branch `chore/tama-ret003-spec` at commit `d05e0a6a92a7c58ad2aae37232c82432a8ae95ec`
(merged via PR3 at commit `e8c116c2bfe474263a53b27b882d5038fd0870b5`, merged at 2026-07-13).
The 5 correction cycles (Yua 2026-07-13 09:39:26, 09:49:53, 09:55:38, 10:00:42, 10:03:03) are all
applied additively. Top-level response variants with mode discriminator; typed `extra` (compat
path); 5-key `RankedScoreComponents` (all required, `extra=forbid`); `RecentScoreComponents = {}`
(exact, never null); `state` / `importance` nullable for missing legacy; `score_kind` declaration;
`provenance_score` exact-table-only; brief=true preservation; corrupt source -> 500 (not 422).
The spec is the contract. Implementation obeys it.
