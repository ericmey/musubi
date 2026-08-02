---
title: "Slice: LiveKit voice — canonical agent tools"
slice_id: slice-livekit-canonical-tools
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Post-1.0"
tags: [section/slices, status/done, type/slice, adapter, livekit, voice, agent-tools]
updated: 2026-08-02
reviewed: true
depends-on: ["[[_slices/slice-retrieve-recent]]"]
blocks: []
---

# Slice: LiveKit voice — canonical agent tools

> Collapse the two voice mixins (`MemoryToolsMixin` active path +
> `MusubiVoiceToolsMixin` dormant path) into one honest runtime mixin.

**Phase:** 8 Post-1.0 · **Status:** `done` (voice PR #11 merged the mixin
collapse on 2026-04-30; later runtime cleanup deliberately removed tool wrappers
whose backing operations were not honestly available)

## Implementation lives in a sibling repo

`github.com/ericmey/openclaw-livekit` — the standalone voice agent monorepo. This slice is the **tracking artifact** for the work in that repo, mirroring the pattern used for [[_slices/slice-adapter-openclaw]]. Code/tests/PR happen there; the contract this slice satisfies is [[07-interfaces/agent-tools]] in this vault.

## Outcome

Voice PR #11 (`5d3aba0`) completed the structural work:

- renamed the live class to `MusubiToolsMixin` and switched agent MROs;
- deleted dormant `tools/src/tools/musubi_voice.py` and its parallel tests;
- preserved the old `MemoryToolsMixin` name for its documented transition window;
- consolidated recent, search, remember, get, and think implementations in the
  one owned module.

Later commits made the runtime surface stricter than the original draft. `5392008`
removed the unimplemented `musubi_get` stub instead of advertising a tool that could
not fetch. `663757e` unexposed `musubi_think` because no real phone consumer used the
route; its implementation remains available behind an honest future wrapper. The
current LLM surface is exactly `musubi_recent`, `musubi_search`, and
`musubi_remember`. This is intentional supersession, not unfinished migration.

The original cross-modal `musubi_recent` requirement was also superseded by the
deployed operator contract: recent is voice-channel chronology, while
`musubi_search` is the explicit cross-channel semantic path. Prompts, implementation,
and tests all state that distinction.

## Specs to implement

- [[07-interfaces/agent-tools]] (the contract)

## Implemented paths (in `openclaw-livekit`, not this repo)

- `tools/src/tools/memory.py` — single `MusubiToolsMixin` implementation.
- `tools/src/tools/musubi_voice.py` — deleted.
- agent composition — one canonical mixin through the shared base.
- SDK and agent tests — runtime registration, recall, remember, greeting, and MRO
  coverage.

## Depends on

- [[_slices/slice-retrieve-recent]] remains a backend opportunity, not a blocker for
  this completed adapter slice. Voice deliberately retains its scoped recent scroll;
  cross-channel recall uses `musubi_search`.

## Unblocks

- _(none in this vault — downstream is operator/agent-config work in openclaw-livekit)_

## Test Contract

Same canonical contract suite as [[_slices/slice-mcp-canonical-tools]]; modality tag for `musubi_remember` is `src:livekit-voice-remember`. Adapter-specific addition:

- [x] **MRO collapse.** Current agents compose one `MusubiToolsMixin`; the dormant
  parallel mixin is deleted.
- [x] **Legacy transition.** The old class alias shipped for the transition window
  and was later removed with the rest of the dead routes.
- [x] **Greeting hook.** `fetch_recent_context` and `musubi_recent` share one scoped,
  recency-ordered implementation. The cross-modal greeting requirement was
  superseded; `musubi_search` owns cross-channel recall.

## Work log

- 2026-08-02 — **Greeting hook bullet disposition:** the original requirement
  "Voice greeting includes recent activity from other modalities" was withdrawn,
  not deferred. The deployed contract deliberately separates voice-channel
  chronology (`musubi_recent` / the greeting hook) from explicit cross-channel
  semantic recall (`musubi_search`). There is no follow-up issue because restoring
  implicit cross-modal greeting injection is not planned; any future proposal
  would be a new contract decision rather than unfinished work from this slice.

## Definition of Done

![[00-index/definition-of-done]] (adapted: code/tests/PR live in
`openclaw-livekit`). Verified against current remote head `eedee72` on 2026-08-02:
SDK 183 passed/5 skipped; Nyla 32/2; Aoi 27; Yua 30; Party 27; Sumi 81;
scripts 3; voicebook-tts 18; voicebook-stream 49.

## Closure record

- 2026-04-30 — voice PR #11 merged as `5d3aba0`.
- 2026-07-09/10 — fake/unconsumed tool wrappers removed by `5392008` and
  `663757e`; the runtime surface became smaller and more truthful.
- 2026-08-02 — current `origin/main` (`eedee72`) inspected in a detached clean
  worktree and the full workspace test command passed after `make sync-venvs`.
