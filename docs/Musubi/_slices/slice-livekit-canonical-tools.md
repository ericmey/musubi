---
title: "Slice: LiveKit voice — canonical agent tools"
slice_id: slice-livekit-canonical-tools
section: _slices
type: slice
status: blocked
phase: "8 Post-1.0"
tags: [section/slices, status/blocked, type/slice, adapter, livekit, voice, agent-tools]
updated: 2026-04-29
reviewed: false
depends-on: ["[[_slices/slice-retrieve-recent]]"]
blocks: []
---

# Slice: LiveKit voice — canonical agent tools

> Collapse the two voice mixins (`MemoryToolsMixin` active path + `MusubiVoiceToolsMixin` dormant v2 path) into a single canonical mixin that conforms to [[07-interfaces/agent-tools]]. Widen `musubi_recent` from voice-only to cross-modal default.

**Phase:** 8 Post-1.0 · **Status:** `blocked` (on [[_slices/slice-retrieve-recent]] for full cross-modal canonical surface; voice has its own existing recency fallback so the slice MAY pick up early)

## Implementation lives in a sibling repo

`github.com/ericmey/openclaw-livekit` — the standalone voice agent monorepo. This slice is the **tracking artifact** for the work in that repo, mirroring the pattern used for [[_slices/slice-adapter-openclaw]]. Code/tests/PR happen there; the contract this slice satisfies is [[07-interfaces/agent-tools]] in this vault.

## Why this slice exists

The voice agent has two parallel implementations today:

- `tools/src/tools/memory.py` — `MemoryToolsMixin`, the live path. Exposes `musubi_recent`, `musubi_search`, `musubi_remember`. `musubi_recent` is **voice-channel only** — Aoi-on-the-phone cannot answer "what was I doing on Claude Code?".
- `tools/src/tools/musubi_voice.py` — `MusubiVoiceToolsMixin`, dormant v2 path. Exposes `musubi_recall`, `musubi_remember`, `musubi_think`. Awaits a "v2 cutover" that ADR 0032 obsoletes.

Per [[13-decisions/0032-agent-tools-canonical-surface]] the canonical surface is one set of names with cross-modal defaults. Two mixins is one too many.

## Specs to implement

- [[07-interfaces/agent-tools]] (the contract)

## Owned paths (in `openclaw-livekit`, not this repo)

- `tools/src/tools/musubi.py` (new) — single canonical mixin implementing all five tools per spec
- `tools/src/tools/memory.py` (modified) — wrap deprecated names as one-release aliases that delegate to the canonical mixin
- `tools/src/tools/musubi_voice.py` (deleted) — superseded; the dormant v2 path is replaced by the canonical mixin
- `agents/{aoi,nyla,party}/agent.py` — switch MRO to the canonical mixin
- `tools/tests/test_*.py` — contract test cases per [[07-interfaces/agent-tools#test-contract]]

## Forbidden paths

- `sdk/musubi_v2_client.py` — no client changes; consumes existing methods. SDK extensions for `mode=recent` are tracked by [[_slices/slice-retrieve-recent]].

## Depends on

- [[_slices/slice-retrieve-recent]] — `musubi_recent` cross-modal needs `mode=recent` on the backend. Until that lands, voice MAY keep its current `_scroll_episodic_recent` fallback and migrate when the backend mode ships.

## Unblocks

- _(none in this vault — downstream is operator/agent-config work in openclaw-livekit)_

## Test Contract

Same canonical contract suite as [[_slices/slice-mcp-canonical-tools]]; modality tag for `musubi_remember` is `src:livekit-voice-remember`. Adapter-specific addition:

- [ ] **MRO collapse.** Each agent (aoi, nyla, party) has a single `MusubiToolsMixin` in its MRO; `MusubiVoiceToolsMixin` and `MemoryToolsMixin` are gone (or are aliasing-only wrappers).
- [ ] **Legacy tool aliases.** `musubi_recall` (the voice-dormant name) and any divergent `musubi_search` parameter shape resolve to the canonical surface for one minor release with deprecation logging.
- [ ] **Greeting hook.** The on-enter prefetch (`fetch_recent_context`) now consumes `musubi_recent` semantics — cross-modal scope, recency-ordered. Voice greeting includes recent activity from other modalities.

## Definition of Done

![[00-index/definition-of-done]] (adapted: code/tests/PR live in `openclaw-livekit`; this slice flips to `done` when the openclaw-livekit PR merges and an integration test confirms cross-modal recent works)
