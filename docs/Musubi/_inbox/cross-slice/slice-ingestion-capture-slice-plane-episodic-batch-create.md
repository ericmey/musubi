---
title: "Add `EpisodicPlane.batch_create` for true batch ingestion"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-ingestion-capture
target_slice: slice-plane-episodic
status: tracked
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tracked_by: "https://github.com/ericmey/musubi/issues/141"
tags: [section/inbox-cross-slice, type/cross-slice, status/tracked]
updated: 2026-04-20
---

> **Tracked as GH Issue #141** — https://github.com/ericmey/musubi/issues/141
> The ticket body below is preserved for audit; new discussion happens on the Issue.


# Add `EpisodicPlane.batch_create` for true batch ingestion

## Source slice

`slice-ingestion-capture` (PR #86).

## Problem

`docs/Musubi/06-ingestion/capture.md` § Batched capture
specifies:

> `POST /v1/memories/batch` accepts up to 100 items at once:
>
> - Embeds them in a single TEI batch (more efficient).
> - Dedups against the index but not against each other (within the
>   batch).
> - Upserts in a single Qdrant call.
> - Returns 202 with a list of `(object_id, state, dedup)` triples.

The current `EpisodicPlane.create` is one-row-at-a-time: every call
does its own embed + dedup probe + upsert. `CaptureService.batch_capture`
loops calling `create` N times, which means N TEI calls + N Qdrant
upserts — the opposite of the spec's optimisation.

## Impact on slice-ingestion-capture

- Test Contract bullets 20 (`test_batch_capture_single_tei_embed_call`)
  and 21 (`test_batch_capture_single_qdrant_upsert`) are currently
  `@pytest.mark.skip` with this ticket cited.
- Bullet 22 (the 100-item-under-1s benchmark) is declared
  out-of-scope but would also benefit from batch-create.
- The HTTP `/v1/memories/batch` endpoint
  (`src/musubi/api/routers/writes_episodic.py::batch_capture`) inherits
  the loop-over-create pattern and emits N rate-limit-bucket "capture"
  consumptions instead of one "batch-write" consumption.

## Requested change

Add a `batch_create` method to `EpisodicPlane`:

```python
async def batch_create(
    self,
    memories: list[EpisodicMemory],
) -> list[EpisodicMemory]:
    """Embed + dedup-probe + upsert N memories in a single TEI batch
    and a single Qdrant upsert call.

    Per-item dedup against the live index still applies; in-batch
    dedup is intentionally NOT done (per spec) so the caller's
    semantics are preserved.
    """
```

Implementation sketch:

1. Single `await self._embedder.embed_dense([m.content for m in memories])`
   call (TEI batch).
2. Same for sparse.
3. Per-row dedup probe in a loop (or a single Qdrant `query_points`
   batch if the client supports it).
4. Single `client.upsert(points=[...])` for the final batch.
5. Return per-row results matching the input order.

## Acceptance

- `EpisodicPlane.batch_create` lands; one TEI call + one Qdrant upsert
  for the whole batch.
- `slice-ingestion-capture` follow-up swaps `CaptureService.batch_capture`'s
  loop for a single `batch_create` call and unskips test bullets 20 + 21.
