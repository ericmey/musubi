---
title: "Slice: retrieval audit — neutral observation harness + red tests"
slice_id: slice-retrieval-audit-observations
section: _slices
type: slice
status: retired
owner: aoi
phase: "Retrieval audit 2026-07-12"
tags: [section/slices, status/retired, type/slice, retrieval, audit, observability]
updated: 2026-07-15
reviewed: true
depends-on: []
issue: 411
blocks: []
---

# Slice: retrieval audit — neutral observation harness + red tests

## Why

Eric refused, three times, to accept "it's working as designed" for a memory plane that
would not return a memory it had just stored. He was right. The audit that followed
(Yua routing; Tama and Shiori diving) has so far reproduced:

- **D1 / P0** — ranked recall defaults `state_filter` to `('matured','promoted')`; every
  fresh write is `provisional` for ≥1h (`maturation.py:287`). **No fleet caller passed
  `state_filter`.** A memory that was successfully stored and verified could not be
  recalled by any canonical tool for an hour. Reproduced independently by Yua, by Shiori
  on her own writes, and by me.
- **RET-002** — `fast` mode returns a hit and never marks it accessed; `blended`/`deep`
  do. `fast` is the SDK default (`sdk/client.py:128`) and the LiveKit voice path
  (`livekit/fast_talker.py:49`).
- **RET-002 / over-marking** — marking tracks *candidacy*, not *delivery*: on a proven
  5-point cohort with `limit=2`, four rows were marked and **two of them were never
  delivered to the caller**.
- **RET-009** — `include_lineage` is not controllable from the wire: the public
  `RetrieveQuery` has no such field, unknown keys are silently ignored, orchestration
  defaults it true.
- **DQ-001** — ranked/recent projection delivers only the first ~300 chars of
  content-or-title, ignores a supplied summary, and the row does not declare that it was
  truncated.

Every one of these fails **silently**. An empty result because of a hidden default is
indistinguishable from an empty result because nothing was there. There is no error, no
metric, no alert. That is why none of it surfaced for months.

## Scope

**This slice builds instruments, not fixes.** Yua (router) has explicitly held all
production changes pending contract decisions (RET-008 durability matrix, RET-003
additive-API ADR, DQ-001 budget/summary policy). Nothing here changes runtime behaviour.

`owns_paths`:
- `tests/retrieve/harness.py`
- `tests/retrieve/observe_*.py`
- `docs/Musubi/_slices/slice-retrieval-audit-observations.md`

`forbidden_paths`:
- `src/musubi/**` — no production code in this slice
- `src/musubi/api/**`, `openapi.yaml`, `proto/` — API is frozen per version; additive
  changes need an ADR and a `slice-api-v*` owner
- `deploy/**`

## Test Contract

The harness must be *inspectable evidence*, not a claim. Three rules, each of which
exists because a probe in this audit lied:

1. **Never measure through a mutating surface.** `GET /episodic/{id}` **increments
   `access_count`**. A probe that reads access data through a GET is measuring its own
   footprints. There is deliberately no `get()` helper — access data comes from the raw
   Qdrant payload.
2. **Absent is not zero.** `Observation.value is None` when a key is missing, never `0`.
   Conflating them is how a field the API never returns became "a quarter of the ranking
   weight is dead."
3. **A probe must prove its own setup.** `seed()` writes random content and then verifies
   against the store that `version=1`, `access_count=0`, `reinforcement_count=0` — and
   **raises** if capture semantically deduped into an existing row. `seed_many(n)` proves
   *n* distinct object_ids or refuses to run. Yua: *"'seeded=5' input calls is not five
   memories"* and *"do not expand the stem list as a probabilistic fix; the instrument
   must verify the postcondition."*

Bullets realised by the first commit (red observations, no assertions about desired
behaviour):

- [x] `observe_ret002_access.py` — per-mode marking of a *delivered* hit (fast/blended/deep)
- [x] `observe_ret002_access.py` — over/under-marking against a proven-distinct cohort
- [x] `observe_ret002_access.py` — `include_lineage` wire control is silently ignored
- [ ] `observe_dq001_projection.py` — supplied summary; absent summary
- [ ] `observe_dq001_projection.py` — key fact at char 301 / 1501 / end-of-content
- [ ] `observe_dq001_projection.py` — declared vs actual length, in **bytes and chars**
- [ ] `observe_dq001_projection.py` — Unicode grapheme boundaries at the cut point
- [ ] `observe_dq001_projection.py` — projection per layer: HTTP → SDK → MCP → LiveKit → Hermes

**No char budget and no summarizer is chosen in this slice.** Those are Yua's contract.

## Status

**`retired`** — this observation-only harness is preserved as historical audit evidence. Issue #411
was closed on 2026-07-15 after each validated finding had a canonical successor Issue/slice; closing
the parent does not claim the unchecked observation bullets above were implemented here.

Successor ownership:

- **DQ-001** (projection truncation) — completed through its canonical DQ-001 slices and PRs.
- **RET-002** (delivery-boundary access accounting) — completed through Issue #500 / PR #508.
- **RET-009** (`include_lineage` wire control) — tracked in its canonical successor lane.
- **DQ-003** — withdrawn/corrected; not a defect.
- **RET-007** (backend failure → empty 200) — closed by its canonical degradation work.

## Work log

- 2026-07-15 — `codex-gpt5`: retired the historical parent after board reconciliation. The landed
  observations remain evidence; production fixes and remaining contracts live in their named
  successor Issues rather than keeping Issue #411 falsely `in-progress`.
