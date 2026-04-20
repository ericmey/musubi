---
title: "Add a non-transition `CaptureEvent` (or relax `LifecycleEvent`) for capture-time provenance"
section: _inbox/cross-slice
type: cross-slice
source_slice: slice-ingestion-capture
target_slice: slice-types
status: resolved
opened_by: vscode-cc-sonnet47
opened_at: 2026-04-19
tags: [section/inbox-cross-slice, type/cross-slice, status/resolved]
updated: 2026-04-19
---

# Add a non-transition `CaptureEvent` (or relax `LifecycleEvent`) for capture-time provenance

## Source slice

`slice-ingestion-capture` (PR #86).

## Problem

`docs/architecture/06-ingestion/capture.md` § Step 6 calls for the
capture path to "emit `LifecycleEvent(provisional → created)`" so the
audit ledger records the row's ingestion provenance. The current
`LifecycleEvent` validator in `src/musubi/types/lifecycle_event.py` is
strictly a **state-transition** event — its
`is_legal_transition("episodic", from_state, to_state)` rejects
`provisional → provisional` (the only honest description of what
capture does, since the row is fresh).

Concretely:

```python
LifecycleEvent(
    object_type="episodic",
    from_state="provisional",
    to_state="provisional",  # rejected: not in legal_next_states
    actor="ingestion-capture",
    reason="capture-created",
    ...
)
```

raises `ValidationError: illegal transition for episodic: provisional ->
provisional`.

## Impact on slice-ingestion-capture

- Test Contract bullet 5 (`test_capture_emits_lifecycle_event`) is
  currently `@pytest.mark.skip` with this ticket cited.
- The capture ledger entry that the spec calls for cannot be written;
  audit provenance for capture is implicit in the row's `created_at` /
  `reinforcement_count` fields rather than explicit in the lifecycle
  ledger.
- Reflection digests that read the ledger (slice-lifecycle-reflection)
  see no per-capture entries — only state changes.

## Requested change

Pick one of two reconciliations:

**Option A (recommended) — add a `CaptureEvent` type.** A separate
event type for create-time provenance, parallel to `LifecycleEvent`.
Stored in the same sqlite ledger via `LifecycleEventSink`'s schema (or
an additional table). Schema:

```python
class CaptureEvent(BaseModel):
    event_id: KSUID
    object_id: KSUID
    object_type: ObjectType
    namespace: Namespace
    occurred_at: datetime
    actor: str
    reason: Literal["capture-created", "capture-merged"]
    correlation_id: str = ""
```

Cleaner separation: state transitions vs creation events are
semantically distinct.

**Option B — relax `LifecycleEvent`'s validator.** Allow
`from_state == to_state` when `from_state == "provisional"` (or for an
explicit `event_kind="creation"` discriminator). Smaller change but
bends the "every event is a transition" invariant.

## Acceptance

- One of A / B is chosen and applied in `src/musubi/types/`.
- `slice-ingestion-capture` follow-up wires the emit in
  `CaptureService.capture` and unskips test bullet 5.

## Resolution

Resolved by PR #113 (`slice-types-followup`).
