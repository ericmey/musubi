---
title: "Slice: OpenClaw plugin — canonical agent tools"
slice_id: slice-openclaw-canonical-tools
section: _slices
type: slice
status: done
owner: aoi-claude-opus
phase: "8 Post-1.0"
tags: [section/slices, status/done, type/slice, adapter, openclaw, agent-tools]
updated: 2026-04-30
reviewed: false
depends-on: ["[[_slices/slice-retrieve-recent]]"]
blocks: []
---

# Slice: OpenClaw plugin — canonical agent tools

> Bring the openclaw-musubi plugin onto the canonical agent-tools surface. Add `musubi_recent`, alias `musubi_recall` → `musubi_search` for one release, drop the alias afterward. `musubi_get` already lands in `openclaw-musubi#24`.

**Phase:** 8 Post-1.0 · **Status:** `done` (shipped via openclaw-musubi PRs #24 + #26 on 2026-04-30; `musubi_recent` ships with the GET /v1/episodic fallback path until [[_slices/slice-retrieve-recent]] lands the cross-channel `mode=recent`) · **Owner:** `aoi-claude-opus`

## Implementation lives in a sibling repo

`github.com/ericmey/openclaw-musubi` — the OpenClaw plugin (TypeScript). This slice is the tracking artifact, mirroring [[_slices/slice-adapter-openclaw]]. PR `#24` already adds `musubi_get` per the canonical contract; this slice extends that work to bring the rest of the plugin onto the canonical surface.

## Why this slice exists

Per [[13-decisions/0032-agent-tools-canonical-surface]] the canonical search tool is `musubi_search`. The OpenClaw plugin currently registers `musubi_recall`. The plugin is also missing `musubi_recent` — Aoi-via-OpenClaw cannot answer "what was I doing across modalities?" without going through the voice surface.

## Specs to implement

- [[07-interfaces/agent-tools]] (the contract)
- The corresponding doc updates in `openclaw-musubi/docs/architecture/overview.md`, `wiring.md`, `README.md`, etc. — same pattern PR `#24` already established for `musubi_get`.

## Owned paths (in `openclaw-musubi`, not this repo)

- `src/tools/recent.ts` (new) — canonical `musubi_recent`
- `src/tools/search.ts` (new) — canonical `musubi_search` body; `recall.ts` becomes a deprecation alias that delegates here
- `src/tools/parameters.ts` — add `RecentParameters`, `SearchParameters`; keep `RecallParameters` for the alias
- `src/plugin/bootstrap.ts` — register the canonical five (`musubi_search`, `musubi_recent`, `musubi_get`, `musubi_remember`, `musubi_think`); `musubi_recall` stays for one release as a deprecation alias
- `tests/tools/*.test.ts` — contract suite cases per spec
- `tests/plugin/bootstrap.test.ts` — update tool-count + registration order

## Forbidden paths

- The Musubi backend (this repo) — no API changes; `mode=recent` is owned by [[_slices/slice-retrieve-recent]]. Until that ships, `musubi_recent` MAY paginate `GET /v1/episodic` as a fallback and migrate when the backend mode lands.

## Depends on

- [[_slices/slice-retrieve-recent]] — for `musubi_recent` cross-modal default. Fallback path acceptable until it lands.

## Unblocks

- _(no downstream slices in this vault)_

## Test Contract

Same contract suite as [[_slices/slice-mcp-canonical-tools]]; modality tag for `musubi_remember` is `src:openclaw-agent-remember`. Plus:

- [ ] **Alias path.** `musubi_recall` invocation forwards to `musubi_search` and emits a deprecation log line. After one minor release, `musubi_recall` is removed from `bootstrap.ts` registration.
- [ ] **Five-tool registration order.** The bootstrap integration test expects `musubi_recall` (alias) + the five canonical tools = 6 total during the deprecation window; 5 after.

## Definition of Done

![[00-index/definition-of-done]] (adapted: code/tests/PR live in `openclaw-musubi`; this slice flips to `done` when both `#24` (already in flight) and the follow-up PR for `musubi_recent` + `musubi_search` rename merge)
