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
`POST /v1/retrieve`. The changes are purely additive: the public top-level shape gains
`state`, `importance`, `score_kind`; the existing `extra.score_components` path gains 2 keys
(importance, provenance) and renames `reinforce` to `reinforcement`; the new `provenance_score`
top-level is added for recent mode; the recent `extra.score_components` becomes exact `{}`.

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
- Corrupt `state` / `importance` source values → 500 (server integrity), not 422.

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


## Closeout gate: Hermes adapter follow-up (per Yua 2026-07-13 10:56:23)

This ADR is a closeout gate for the broader wire contract. Once the
Musubi contract is stable, the Hermes adapter
(`hermes-agent/agent/memory_provider.py`, the `musubi_recall` JSON) must
preserve the following through without fabricating fields:

- **Ranked mode**: `result["results"][i]["state"]` (LifecycleState enum, 7 values,
  nullable for missing legacy), `result["results"][i]["importance"]` (int 1..10,
  nullable for missing legacy), and the full 5-key
  `result["results"][i]["extra"]["score_components"]` (relevance, recency,
  importance, provenance, reinforcement).
- **Recent mode**: `result["results"][i]["score_kind"]` is `"created_epoch"`, and
  `result["results"][i]["provenance_score"]` (nullable, exact-table-only).

The expected transformation is: the Hermes `musubi_recall` JSON may add
a top-level `state` field (nullable) and an `importance` field
(nullable) alongside the existing `result_id`; the `score_components`
dict is already there in `extra` and should be passed through verbatim.
The Hermes adapter must NOT fabricate values; it must null through for
missing-legacy fields and pass the `score_components` dict through
unchanged.

This follow-up is a separate slice/branch (NOT this one). It is a
"closeout gate" for the broader wire contract, secondary to Musubi
correctness. This ADR documents the contract; the Hermes adapter lands
in a follow-up that depends on this slice's wire contract.
