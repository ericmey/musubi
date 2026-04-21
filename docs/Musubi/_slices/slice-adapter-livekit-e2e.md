---
title: "Slice: LiveKit adapter end-to-end tests against live Musubi"
slice_id: slice-adapter-livekit-e2e
section: _slices
type: slice
status: done
owner: claude-code-opus-4-7
phase: "7 Adapters"
tags: [section/slices, status/done, type/slice, livekit, adapter, integration]
updated: 2026-04-21
reviewed: false
depends-on: ["[[_slices/slice-adapter-livekit]]", "[[_slices/slice-ops-integration-harness]]"]
blocks: []
---

# Slice: LiveKit adapter end-to-end tests against live Musubi

> The LiveKit adapter shipped with 26 unit tests against a `FakeMusubiClient`
> — the [[_slices/slice-adapter-livekit]] spec's bullets 17–19 declared
> real-LiveKit + real-Musubi integration out-of-scope pending a docker-up
> harness. The harness landed with [[_slices/slice-ops-integration-harness]];
> this slice closes the integration gap.

**Phase:** 7 Adapters · **Status:** `done` · **Owner:** `claude-code-opus-4-7`

## Why this slice exists

Two separate quality gaps:

1. **Adapter ↔ Musubi contract drift.** The adapter is structurally typed
   against `MusubiClient` (`src/musubi/adapters/livekit/adapter.py`). When
   the SDK shape changes, `FakeMusubiClient` is hand-updated to track —
   no runtime check prevents drift. An integration test that exercises
   the real adapter against the real `AsyncMusubiClient` → real Musubi →
   real Qdrant is the only thing that catches this.

2. **Event-to-capture semantics.** The adapter's four entrypoints
   (`on_transcript_segment`, `on_user_turn_completed`, `on_session_end`,
   `maybe_capture_fact`) each eventually call the Musubi capture/thoughts
   APIs. The unit tests assert the *calls happen*; they don't assert the
   *result persists* — a capture that fails pydantic validation at the
   API layer, or gets silently deduped, or lands in the wrong namespace,
   is invisible to the unit tests.

Not in scope: a real LiveKit server. LiveKit's WebRTC protocol is beyond
the adapter's abstraction — the adapter doesn't implement LiveKit protocol,
it responds to events from `livekit-agents`. Testing the
`livekit-agents` → `LiveKitAdapter` edge is an upstream-SDK concern;
testing the `LiveKitAdapter` → `Musubi` edge is ours.

## Specs to implement

- [[07-interfaces/livekit-adapter]] § Test contract, bullets 17–19
- [[_slices/slice-ops-integration-harness]] — provides the `live_stack` fixture

## Owned paths (you MAY write here)

- `tests/integration/test_livekit_e2e.py` (new)
- `tests/integration/_livekit_fixtures.py` (new — synthetic event generators)

## Forbidden paths

- `src/musubi/adapters/livekit/**` — this slice tests, it doesn't modify.
- `deploy/test-env/docker-compose.test.yml` — stack is already configured.

## Definition of Done

![[00-index/definition-of-done]]

Plus:

- [ ] A synthetic LiveKit transcript sequence ("turn start → interim
      segments → final segment → turn end") captured against live Musubi
      results in at least one episodic row and one thought.
- [ ] `redact_pii` runs before upload — an email in the transcript does
      not appear in the persisted episodic.
- [ ] Musubi's capture-side dedup collapses duplicate fact captures —
      two identical utterances in quick succession produce one
      persisted row, either merged (same object_id) or dedup-reinforced
      (second response carries a `dedup` signal). The ContextCache
      lives on the retrieve path, not the capture path, so this bullet
      validates Musubi's capture pipeline behavior end-to-end rather
      than a cache short-circuit.
- [ ] `maybe_capture_fact` only fires when `detect_interesting_fact`
      returns True — a generic filler phrase doesn't produce a capture.
- [ ] A session's `on_session_end` writes a session-summary thought that
      is retrievable via the thoughts plane.

## Test contract (copy to code)

```
bullet 1: test_e2e_full_turn_persists_episodic_and_thought
bullet 2: test_e2e_redaction_strips_email_before_capture
bullet 3: test_e2e_capture_side_dedup_collapses_duplicate_facts
bullet 4: test_e2e_filler_phrase_does_not_capture
bullet 5: test_e2e_session_end_emits_retrievable_thought
```

## Work log

### 2026-04-21 — claude-code-opus-4-7

Slice spec drafted. Existing integration harness (`tests/integration/conftest.py::live_stack`) already provides a docker-compose Musubi + real Qdrant + real TEI/Ollama — the new test file reuses it directly. Synthetic LiveKit event generators in `_livekit_fixtures.py` emit the four adapter entrypoints with realistic payloads.

### 2026-04-21 — claude-code-opus-4-7 (handoff)

Tests landed at [tests/integration/test_livekit_e2e.py](../../tests/integration/test_livekit_e2e.py) + [tests/integration/_livekit_fixtures.py](../../tests/integration/_livekit_fixtures.py). All five bullets implemented 1-to-1. Local gates: `ruff check`, `mypy --strict`, `make check` (unit + coverage), `make agent-check` — all green (warnings only: two pre-existing "depends-on not reverse-linked as blocks" on target slices I can't edit from this slice's owns_paths).

**Spec adjustment.** Bullet 3 originally said *"ContextCache short-circuits duplicate captures"* — incorrect: the `ContextCache` in the adapter is a retrieve cache, not a capture gate. Revised bullet asserts the end-to-end semantic that actually holds: Musubi's capture pipeline collapses two identical captures into one persisted row, via either merge or dedup-signal response. Commit trailer: `spec-update: docs/Musubi/_slices/slice-adapter-livekit-e2e.md`.

**Out-of-band finding.** `make tc-coverage SLICE=<id>` points at `docs/architecture/_slices/` but the vault lives at `docs/Musubi/_slices/` — tool path bug in [docs/Musubi/_tools/tc_coverage.py:45](../_tools/tc_coverage.py) (`VAULT = ROOT / "docs" / "architecture"`). Not in this slice's owned_paths; flagging here so the next slice with `_tools/**` ownership can fix. Closure-Rule audit performed manually: all 5 bullets are in the passing state (one test per bullet), none skipped, none out-of-scope.

**Not in scope.** A real LiveKit SFU. The WebRTC edge belongs to `livekit-agents` upstream; the `LiveKitAdapter → Musubi` edge is ours and is what these tests cover.

## PR links

- https://github.com/ericmey/musubi/pull/184 — initial test contract + handoff
