---
title: "Slice: H9 LiveKit Transcript Fallback Data Loss"
slice_id: slice-h9-livekit-transcript-fallback
issue: 461
section: _slices
type: slice
status: in-progress
owner: shiori
phase: "Retrieval"
tags: [section/slices, status/in-progress, type/slice]
updated: 2026-07-14
---

# Slice: H9 LiveKit Transcript Fallback Data Loss

**Tracks Issue:** #461

## Problem
When `LiveKitAdapter.on_session_end` lacks an `_upload_handler` (the fallback path), it calls `episodic.capture`. The adapter scrubs the `vtt_transcript` but discards it, instead hardcoding `content=f"[transcript:{session_id}]"`. Any voice session operating on this fallback path completely loses its transcript context, impairing downstream episodic memory.

## Independence & Overlap
- **Overlap Report:** This defect is isolated to `src/musubi/adapters/livekit/adapter.py:168-173`. There is no overlap with active lanes (C6b atomicity, ART-001 single-generation chunks, RET-007 degradation, or ADAPT-001 CLI overrides). 
- **Consumer Impact:** The data loss uniquely impacts LiveKit voice deployments (like Aoi's `aoi/voice/episodic`) when the canonical artifact upload handler is absent.
- **Related Issues:** Issue #285 (`slice-livekit-canonical-tools`) is unrelated to this specific write-path payload drop and remains blocked.

## Tests-First Acceptance Contract (Strict Red)
1. **Red Contract:** Replace the existing vacuous `test_transcript_fallback_capture_adds_typed_episode_tags` with a red-contract test (must `strict-xfail`) asserting that the fallback `episodic.capture` receives the exact scrubbed UTF-8 transcript as `content`, instead of the `[transcript:...]` stub. Existing typed tags and `session_id` must be preserved.
2. **Control - Handler Success:** Verify that when `_upload_handler` is present and succeeds, the fallback capture is not triggered.
3. **Control - Empty Transcript:** Verify that an empty or whitespace-only transcript is explicitly skipped rather than passing a useless stub or dispatching an invalid payload (which guarantees a 422 API rejection).

**Owned Paths:**
- `src/musubi/adapters/livekit/adapter.py`
- `tests/adapters/test_livekit.py`
