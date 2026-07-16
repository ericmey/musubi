---
title: "ADR 0035: Additive API Contract — RET-003 ranked vs recent wire shape"
section: 13-decisions
tags: [adr, api, area:retrieval, wire-contract, architecture, section/decisions, status/proposed, type/adr]
type: adr
status: proposed
date: 2026-07-13
deciders: [Eric]
updated: 2026-07-13
up: "[[13-decisions/index]]"
reviewed: false
---

# ADR 0035: Additive API Contract — RET-003 ranked vs recent wire shape

**Status:** proposed
**Date:** 2026-07-13
**Deciders:** Eric

## Context

The current `/v1/retrieve` public wire is missing several fields the implementation produces:

| Field | Current state | Desired |
|---|---|---|
| top-level `state` (LifecycleState enum, 7 values) | omitted | required (nullable for legacy) |
| top-level `importance` (int 1..10) | omitted | required (nullable for legacy) |
| top-level `score_kind` | omitted | required (string enum) |
| `extra.score_components` (ranked) | 3 keys (relevance, recency, reinforcement) | 5 keys (added importance, provenance; public `reinforcement` name) |
| `extra.score_components` (recent) | fabricated `{relevance: 0, recency: 1, reinforcement: 0}` | exact `{}` typed (never `null`) |
| top-level `provenance_score` (recent) | omitted | required (nullable; exact-table-only) |

The implementation produces these values internally — they just don't make it onto the public
wire. Aoi's spec-source read at harem-ops commit `e8c116c2` (merging `d05e0a6`) on branch
`chore/tama-ret003-spec` via PR3, file
`projects/active/hermes-musubi-provider/specs/spec-ret003-ranked-recent-wire-contract.md` is
the authoritative locked contract for the public shape. The 5 correction cycles
(Yua 2026-07-13 09:39:26, 09:49:53, 09:55:38, 10:00:42, 10:03:03) are all applied.

## Decision

Adopt the RET-003 public wire shape from the harem-ops spec as the additive-API contract for
`POST /v1/retrieve`. The changes are **additive fields + a deliberate
corrective semantic change**, not "purely additive" (Yua 2026-07-13
11:57:59 #8):

- **Additive**: the public top-level shape gains `state`,
  `importance`, `score_kind`; the existing `extra.score_components`
  path gains 2 keys (importance, provenance) and uses the public name
  `reinforcement`; the new `provenance_score` top-level is added for
  recent mode.
- **Deliberate corrective change (NOT purely additive)**: the
  recent-mode `extra.score_components` changes from a fabricated
  3-key object `{relevance: 0, recency: 1, reinforcement: 0}` to
  the exact empty `{}` (typed `RecentScoreComponents(extra='forbid')`).
  Any consumer reading the prior fabricated keys will see them
  disappear. This is a **breaking semantic change** for those
  consumers, named explicitly here so the change is not mis-described
  as purely additive.

**Compat risk (must be addressed by adapter tests before deploy):**

- The Hermes adapter (`/Users/ericmey/Vaults/fleet-tools/hermes-plugins/musubi/__init__.py`)
  currently discards `extra` entirely; the Hermes closeout gate covers ranked passthrough.
  A future recent-mode Hermes surface MUST be tested against the
  exact-`{}` shape so it does not depend on the prior fabricated keys.
- Any internal Musubi consumer that reads `recent_results[i].extra.score_components.relevance`
  (or recency / reinforcement) will silently see `None` after the change; this ADR
  requires those consumers to be updated in lockstep with the
  implementation slice.
- The repo-root `openapi.yaml` is a committed deploy-time snapshot
  and MUST be regenerated as a **blocking tracked dependency** of
  the implementation slice (not an unnamed later task). The
  implementation slice MUST keep runtime vs snapshot parity; if the
  regen lands later, the slice is not ACCEPTED.

The discriminator is at the **top-level response** (`mode` field on the new top-level variants
`RankedRetrieveResponse` and `RecentRetrieveResponse`), not on individual rows.

The compat path is preserved: `extra.score_components` is retained at the same path with the same
5-key shape for ranked mode; recent mode keeps `{}` at the same path.

Runtime Pydantic is the authoring truth. The repo-root `openapi.yaml` is the committed
deploy-time snapshot. The docs skeleton `docs/Musubi/07-interfaces/openapi/musubi.v1.yaml` is
**untouched** in this slice (it is regenerated only as a separate `slice-api-v*` ADR step).

## Schema

Two top-level response variants (discriminated by `mode`):

```python
RankedRetrieveResponse(BaseModel):
    mode: Literal["fast", "deep", "blended"]      # top-level discriminator
    results: list[RankedResultRow]
    limit: int
    warnings: list[str] = []

RecentRetrieveResponse(BaseModel):
    mode: Literal["recent"]                          # top-level discriminator
    results: list[RecentResultRow]
    limit: int
    warnings: list[str] = []
```

Two row schemas (rows have NO `mode` field):

```python
class RankedScoreComponents(BaseModel):
    model_config = ConfigDict(extra='forbid')
    relevance:      float = Field(..., ge=0.0, le=1.0)
    recency:        float = Field(..., ge=0.0, le=1.0)
    importance:     float = Field(..., ge=0.0, le=1.0)
    provenance:     float = Field(..., ge=0.0, le=1.0)
    reinforcement:  float = Field(..., ge=0.0, le=1.0)

class RankedExtra(BaseModel):
    score_components: RankedScoreComponents
    lineage: dict[str, Any] | None = None

class RankedResultRow(BaseModel):
    object_id:  str
    namespace:  str
    plane:      Literal["episodic", "curated", "concept", "artifact"]
    score:      float
    title:      str | None = None
    content:    str
    state:      LifecycleState | None = None       # nullable
    importance: int | None = Field(None, ge=1, le=10)  # nullable
    score_kind: Literal["ranked_combined"]
    extra:      RankedExtra
```

```python
class RecentScoreComponents(BaseModel):
    model_config = ConfigDict(extra='forbid')  # exact {}, never null
    # no fields declared; serialization is exactly {}

class RecentExtra(BaseModel):
    score_components: RecentScoreComponents = RecentScoreComponents()  # exact {}
    lineage: dict[str, Any] | None = None

class RecentResultRow(BaseModel):
    object_id:  str
    namespace:  str
    plane:      Literal["episodic", "curated", "concept", "artifact"]
    score:      float
    score_kind: Literal["created_epoch"]
    title:      str | None = None
    content:    str
    state:      LifecycleState | None = None       # nullable
    importance: int | None = Field(None, ge=1, le=10)  # nullable
    provenance_score: float | None = None
    extra:      RecentExtra
```

## Compat

- `extra.score_components` is preserved at the same path. The existing test
  `tests/api/test_api_v0_read.py:869-905::test_retrieve_result_carries_score_components_in_extra`
  is updated from asserting 3 keys to asserting 5 keys (compat path).
- The public name is `reinforcement` (full word) on the wire. The internal scoring model
  retains the singular `reinforce` field name (no internal rename; the public boundary
  mapping is at the orchestration layer).
- `RecentScoreComponents` is exactly `{}` (OpenAPI `additionalProperties: false`). Any
  non-empty input fails the Pydantic model validation → 500 (not 422). This is a server-side
  data integrity failure, not a request validation failure.

## Implementation gates

- All 18 acceptance tests in the spec (§6) must pass.
- The 1 existing test (`test_retrieve_result_carries_score_components_in_extra`) is updated to
  assert 5 keys (compat path) — that migration lands in the implementation slice, not the
  tests-first slice.
- All 6 existing regression-guard tests remain unchanged.
- Runtime `/v1/openapi.json` is the source-of-truth; repo-root `openapi.yaml` is regenerated
  as a separate slice.
- Corrupt `state` / `importance` source values → 500 (server integrity), not 422. **SUPERSEDED for
  RANKED reads — see §DATA-001 P2 supersession below.**

## DATA-001 P2 supersession (2026-07-16)

The accepted DATA-001 Phase 2 ADR
([data001-phase2-immutable-vectors](data001-phase2-immutable-vectors.md)) freezes the rule that a
malformed **ranked** candidate is **skipped** (fail closed) during anchor-aware retrieval — never
500-ing the whole query over one corrupt row. DATA-001 P2 is the later accepted contract, so it
supersedes the RET-003 B1 "corrupt-source → 500" rule **for ranked reads only**: a corrupt `state` /
`importance` source value now yields **HTTP 200 with the bad row OMITTED** from the results (never
fabricated, never exposed). **Identity reads** (the curated vault-path resolution and
`scan_vault_rows`) remain **fail-loud** — a broken identity there raises or surfaces a typed
`invalid_row`, because a silently-dropped identity would let `create` duplicate or the reconciler
archive on an incomplete inventory. Tests updated:
`test_retrieve_ranked_state_is_source_backed_not_fabricated` and
`test_retrieve_ranked_importance_is_source_backed_not_fabricated` now assert 200 + omitted.

## Slice ownership (for the tests-first slice)

The tests-first slice (`slice-api-v1-ret003-wire`, Issue #435) lives at
`docs/Musubi/_slices/slice-api-v1-ret003-wire.md`. The first commit is tests-first,
**zero `src/`**. The 18 acceptance tests land in a single new test file
`tests/api/test_retrieve_ret003_wire.py`.

The implementation slice lands in a follow-up branch after the test contract is accepted.

## Out of scope

- Aoi C6 work (separate lane, Issue #433)
- Shiori's RET-004 (separate slice: `slice-ret004-evals`, Issue #430)
- `/v1/retrieve/stream` (RET-010 surface; out of scope for RET-003)
- `docs/Musubi/07-interfaces/openapi/musubi.v1.yaml` (stale skeleton; not hand-edited in
  this slice; regenerated as a separate `slice-api-v*` ADR step)
- SEC-005 binding-trace (separate lane)
- Nyla / Sumi consumer proof (separate lane)


## Closeout gate: Hermes adapter follow-up (per Yua 2026-07-13 10:56:23; corrected 11:19:50 + 11:57:59 #7)

This ADR is a closeout gate for the broader wire contract. Once the
Musubi contract is stable, the Hermes adapter
(`/Users/ericmey/Vaults/fleet-tools/hermes-plugins/musubi/__init__.py`,
lines ~1200-1305 — a standalone Hermes user plugin loaded as such,
NOT core/MCP) must preserve the following through without fabricating
fields. Per Yua 2026-07-13 11:19:50 + 11:57:59 #7 correction, the
current emitted shape is:

- The plugin emits Musubi's **logical API `object_id` (the stored KSUID)**,
  NOT the physical Qdrant point id. `episodic_point_id(object_id)` is a
  distinct UUID translation used internally to address the Qdrant
  point; the plugin emits the KSUID on the wire.
- The plugin discards `extra` entirely today; it does NOT already
  pass `score_components` through.
- `musubi_recall` is pinned to BLENDED ranked mode today; recent mode
  is NOT a current surface in the plugin.
- Recent passthrough is therefore only relevant if a future Hermes
  surface requests recent.

For the follow-up:

- **Ranked mode (the only current surface)**: the Hermes adapter
  must surface `state` (LifecycleState enum, 7 values, nullable for
  missing legacy) and `importance` (int 1..10, nullable for missing
  legacy) on the JSON row alongside the existing `object_id` (the
  KSUID, not the physical Qdrant point id); and must pass the 5-key
  `extra.score_components` dict (relevance, recency, importance,
  provenance, reinforcement) through without fabrication. The adapter
  must NOT fabricate values; it must null through for missing-legacy
  fields.
- **Recent mode**: only relevant if a future Hermes surface requests
  recent. When that lands, the adapter must surface
  `score_kind="created_epoch"` and `provenance_score` (nullable,
  exact-table-only).

This follow-up is a separate slice/branch (NOT this one). It is a
"closeout gate" for the broader wire contract, secondary to Musubi
correctness. This ADR documents the contract; the Hermes adapter
lands in a follow-up that depends on this slice's wire contract.
