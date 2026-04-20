---
title: "Add a merge-strategy parameter to `EpisodicPlane.create`/`_reinforce`"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-ingestion-capture
target_slice: slice-plane-episodic
status: tracked
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tracked_by: "https://github.com/ericmey/musubi/issues/142"
tags: [section/inbox-cross-slice, type/cross-slice, status/tracked]
updated: 2026-04-20
---

> **Tracked as GH Issue #142** — https://github.com/ericmey/musubi/issues/142
> The ticket body below is preserved for audit; new discussion happens on the Issue.

# Add a merge-strategy parameter to `EpisodicPlane.create`/`_reinforce`

## Source slice

`slice-ingestion-capture` (PR #86).

## Problem

`docs/Musubi/06-ingestion/capture.md` § Step 4 — Dedup specifies:

> Update content if the new content is strictly longer (more detail
> wins).

The current `EpisodicPlane._reinforce` (in
`src/musubi/planes/episodic/plane.py`) unconditionally replaces the
existing row's content with the new text:

```python
data.update(
    content=new.content,
    tags=merged_tags,
    reinforcement_count=existing.reinforcement_count + 1,
    ...
)
```

This is the "always-new-wins" strategy, not the spec's "longer-wins"
strategy.

## Impact on slice-ingestion-capture

- Test Contract bullet 10 (`test_dedup_keeps_longer_content`) is
  currently `@pytest.mark.skip` with this ticket cited.
- A short follow-up capture would silently overwrite a longer earlier
  one — the opposite of what the spec calls for ("more detail wins").

## Requested change

Add a `merge_strategy` parameter to `EpisodicPlane.create`:

```python
async def create(
    self,
    memory: EpisodicMemory,
    *,
    merge_strategy: Literal["replace", "longer-wins"] = "longer-wins",
) -> EpisodicMemory: ...
```

The `_reinforce` helper picks the kept content based on the strategy:

- `"replace"` — current behaviour (new wins).
- `"longer-wins"` — keep `existing.content` if `len(existing.content)
  > len(new.content)`, else use `new.content`.

Default flips to `"longer-wins"` to match the spec; callers that want
the old behaviour pass `"replace"` explicitly.

## Acceptance

- `EpisodicPlane.create` accepts the new parameter; default is
  `"longer-wins"`.
- `slice-ingestion-capture` follow-up unskips test bullet 10 and
  passes `merge_strategy` from `CaptureService.capture` (or relies on
  the new default).
