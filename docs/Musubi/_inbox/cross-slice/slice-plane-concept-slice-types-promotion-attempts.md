---
title: "Add `promotion_attempts: int` and `last_reinforced_at: datetime | None` to `SynthesizedConcept`"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-plane-concept
target_slice: slice-types
status: resolved
opened_by: vscode-cc-opus47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

# Add `promotion_attempts: int` and `last_reinforced_at: datetime | None` to `SynthesizedConcept`

## Source slice

`slice-plane-concept` (PR #42).

## Problem

The spec at `docs/Musubi/04-data-model/synthesized-concept.md`
declares two fields on `SynthesizedConcept` that the model in
`src/musubi/types/concept.py` does not:

- `promotion_attempts: int = 0` (spec §Pydantic model + §Promotion gate)
- `last_reinforced_at: datetime | None = None` (spec §Pydantic model)

Both fields are referenced by indexes in `src/musubi/store/specs.py`
(`_CONCEPT_DELTAS` for `last_reinforced_epoch`, the index for
`promotion_attempts`).

`MusubiObject.model_config` is `extra="forbid"`, so any write attempting
to populate either field fails with `ValidationError`.

## Impact on slice-plane-concept

- `ConceptPlane.reinforce` cannot record `last_reinforced_at` —
  reinforcement_count + version still bump, but the timestamp the
  spec calls for is lost.
- `ConceptPlane.record_promotion_rejection` cannot bump
  `promotion_attempts`.
- The `slice-lifecycle-promotion` retry-backoff predicate
  (`promotion_attempts < 3`, per spec §Promotion gate) and any
  reinforcement-staleness analytics (e.g.
  `slice-lifecycle-maturation`'s 30-day demotion timer) cannot be
  evaluated against these fields.
- Test Contract bullets 15, 16, and 22 (and any maturation/promotion
  bullet that needs to read the timestamps) are constrained: bullet 16
  is implemented at the plane today *without* a `last_reinforced_at`
  assertion. Bullet 22 is already deferred to
  `slice-lifecycle-promotion` but that slice will need
  `promotion_attempts` first.

## What this slice did instead

`slice-plane-concept` ships without either field. The plane methods set
the rejected-side timestamp + reason and bump
reinforcement_count + version, but do not store
`last_reinforced_at` or `promotion_attempts`. The plane work log notes
the gap.

## Requested change

Add to `src/musubi/types/concept.py`:

```python
last_reinforced_at: datetime | None = None
promotion_attempts: int = Field(default=0, ge=0)
```

The accompanying validator should ensure `last_reinforced_at` is
timezone-aware (call `ensure_utc` like the other optional datetimes).
No store-side change is needed — both indexes already exist.

## Acceptance

- `SynthesizedConcept(merged_from=[...], ..., promotion_attempts=2, last_reinforced_at=utc_now())` validates.
- `ConceptPlane.reinforce` + `ConceptPlane.record_promotion_rejection`
  (this slice) can be updated in a follow-up PR to populate them.

## Resolution

Resolved by PR #113 (`slice-types-followup`).
