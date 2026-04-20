---
title: "Add `importance_last_scored_at: datetime | None` to `EpisodicMemory`"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-lifecycle-maturation
target_slice: slice-types
status: resolved
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

# Add `importance_last_scored_at: datetime | None` to `EpisodicMemory`

## Source slice

`slice-lifecycle-maturation` (PR #52).

## Problem

`docs/architecture/06-ingestion/maturation.md` § Re-enrichment on next
sweep specifies a secondary maturation path:

```sql
WHERE state = 'matured' AND (importance_last_scored_at IS NULL OR
                              importance_last_scored_at < now - 7d)
LIMIT 100
```

This is the spec's mechanism for catching memories that were matured
during an Ollama outage (state moved to `matured` without enrichment)
and re-running the LLM scoring on them every week.

The pydantic model in `src/musubi/types/episodic.py` (and its parent
`MemoryObject` in `src/musubi/types/base.py`) does not declare
`importance_last_scored_at`. `MusubiObject.model_config` is
`extra="forbid"`, so any sweep attempting to write the field would fail
with `ValidationError`. The `UNIVERSAL_INDEXES` in
`src/musubi/store/specs.py` does not declare an index on the field
either, so the secondary `WHERE` predicate above has no payload index
to back it.

## Impact on slice-lifecycle-maturation

- The re-enrichment sweep cannot be implemented; it would need to write
  `importance_last_scored_at` after each successful score and read it
  in the secondary selection.
- Bullet 24 (`integration: ollama-offline scenario — maturation
  completes without enrichment, re-enrichment sweep picks them up
  later`) is declared out-of-scope in this slice's work log specifically
  citing this missing field.
- Bullet 17 (`test_ollama_outage_still_matures_without_enrichment`)
  passes today because the *primary* sweep transitions state without
  enrichment; the *secondary* sweep that picks the row up later cannot
  be implemented until this field exists.

## What this slice did instead

Shipped without the field. Primary maturation sweep transitions state
even when Ollama is unavailable (per spec failure-mode contract).
Re-enrichment sweep is not implemented; the bullet is declared
out-of-scope in `_slices/slice-lifecycle-maturation.md ## Work log` with
this ticket cited as the blocker.

## Requested change

Add to `src/musubi/types/episodic.py`:

```python
importance_last_scored_at: datetime | None = None
```

The accompanying validator should ensure the timestamp is timezone-aware
(call `ensure_utc` like the other optional datetimes on the model).

Add to `src/musubi/store/specs.py::_EPISODIC_DELTAS`:

```python
IndexSpec(field_name="importance_last_scored_epoch", schema="float"),
```

(The `_epoch` mirror lets the secondary sweep filter via `Range` on the
keyword/float-indexed path the rest of the spec uses for
`updated_epoch` / `created_epoch`.)

## Acceptance

- `EpisodicMemory(..., importance_last_scored_at=utc_now())` validates.
- `_EPISODIC_DELTAS` indexes the `_epoch` mirror.
- `slice-lifecycle-maturation` follow-up adds the secondary
  re-enrichment sweep + a bullet-24 unit test that exercises the
  outage-then-re-enrich path end-to-end against an in-memory Qdrant.

## Resolution

Resolved by PR #113 (`slice-types-followup`).
