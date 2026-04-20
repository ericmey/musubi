---
title: "Slice: LiveKit adapter"
slice_id: slice-adapter-livekit
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "6 Lifecycle"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-sdk-py]]", "[[_slices/slice-retrieval-fast]]", "[[_slices/slice-retrieval-deep]]"]
blocks: []
---

# Slice: LiveKit adapter

> LiveKit Agents toolkit: Fast Talker + Slow Thinker pattern. `on_user_turn_completed` hook. Hard 200ms budget.

**Phase:** 6 Lifecycle · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[07-interfaces/livekit-adapter]]

## Owned paths (you MAY write here)

- `src/musubi/adapters/livekit/`
- `tests/adapters/test_livekit.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/sdk/`         (owned by slice-sdk-py, done — CALL, don't modify)
- `src/musubi/adapters/mcp/` (owned by slice-adapter-mcp, may be in-progress)
- `src/musubi/adapters/openclaw/` (reserved for slice-adapter-openclaw)
- `src/musubi/retrieve/`    (owned by retrieval DAG, all done — CALL via the SDK)
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/types/`
- `src/musubi/ingestion/`
- `src/musubi/lifecycle/`
- `openapi.yaml`
- `proto/`

> **Spec drift note (reviewer convention):** [[07-interfaces/livekit-adapter]]
> opens with "Independent project. Repo: musubi-livekit-adapter. Embedded into
> the LiveKit agent worker as a Python package." Per ADR-0015, the adapter
> lives in-monorepo at `src/musubi/adapters/livekit/` and imports as
> `musubi.adapters.livekit`. Update the spec in-PR with a
> `spec-update: docs/Musubi/07-interfaces/livekit-adapter.md` commit
> trailer — same pattern VS Code used on slice-sdk-py.

## Depends on

- [[_slices/slice-sdk-py]] (done — wraps every Musubi call)
- [[_slices/slice-retrieval-fast]] (done — Fast Talker path, ~150ms budget)
- [[_slices/slice-retrieval-deep]] (done — Slow Thinker path, ~2s budget)

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- _(no downstream slices)_

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] Every Test Contract item in the linked spec(s) is a passing test.
- [ ] Branch coverage ≥ 85% on owned paths (90% for `musubi/planes/**` and `musubi/retrieve/**`).
- [ ] Slice frontmatter flipped from `ready` → `in-progress` → `in-review` → `done`.
- [ ] Spec `status:` updated if prose changed (`spec-update: <path>` commit trailer).
- [ ] Lock file removed from `_inbox/locks/`.

## Work log

Agents append one entry per work session. Format:
`### YYYY-MM-DD HH:MM — <agent-id> — <what changed>`

### 2026-04-17 — generator — slice created

- Seeded from the roadmap + guardrails matrix.

### 2026-04-19 — operator — reconcile paths to post-ADR-0015 monorepo layout

- 8th pre-src-monorepo drift fix. `owns_paths` was `musubi-livekit/` (pre-monorepo external-repo layout); reconciled to `src/musubi/adapters/livekit/` + `tests/adapters/test_livekit.py` per ADR-0015 §Decision.
- `forbidden_paths` expanded from `musubi/` + `musubi-sdk-py/` to the full post-monorepo list, including sibling adapters so the three-way concurrent-adapter scenario doesn't collide.
- Added `[[_slices/slice-retrieval-deep]]` to `depends-on` (Slow Thinker uses deep-path — was implicitly required but not declared).
- Spec [[07-interfaces/livekit-adapter]] still references the external-repo layout (`musubi-livekit-adapter`); implementing agent updates the spec in-PR with a `spec-update:` trailer.

### 2026-04-19 — vscode-cc-sonnet47 — take

- Claimed atomically via `gh issue edit 3 --add-assignee @me` + label flip `status:ready → status:in-progress` (dual-update before writes, post-#93 drift is now `✗` not `⚠`).
- Branch `slice/slice-adapter-livekit` off `v2`.
- Same agent that landed slice-sdk-py (#90) — the SDK surface is fresh context, so the Fast Talker + Slow Thinker wiring against `AsyncMusubiClient` lands without re-reading the SDK.

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Implemented `src/musubi/adapters/livekit/` (8 modules: `__init__`, `config`, `cache`, `slow_thinker`, `fast_talker`, `heuristics`, `redaction`, `adapter`). `LiveKitAdapter` is the per-session orchestrator wiring `on_transcript_segment` / `on_user_turn_completed` / `on_session_end` / `maybe_capture_fact` to the SDK.
- 23 unit tests; all 16 testable Test Contract bullets pass; 3 integration bullets (17-19) declared out-of-scope per the work log (need docker-up Musubi + LiveKit session simulator). Branch coverage on owned code: 91% (gate 85%).
- Spec rename `musubi-livekit-adapter` → `musubi.adapters.livekit` per ADR-0015 / ADR-0016 applied in same PR with `spec-update: docs/Musubi/07-interfaces/livekit-adapter.md` trailer on the feat commit.
- One cross-slice ticket opened: `slice-adapter-livekit-slice-sdk-py-async-fake.md` (promote the adapter-local `_AsyncFake` shim into `musubi.sdk.testing` as `AsyncFakeMusubiClient`; MCP + OpenClaw will need the same).
- Handoff checks: `make check` green (814 passed, 201 skipped), `make tc-coverage SLICE=slice-adapter-livekit` reports closure satisfied, `make agent-check` clean (warnings only — none touching this slice; the two outstanding ⚠ are about Nyla/Hana's parallel slices), feat commit landed with `spec-update:` trailer.
- Flipping `status: in-review`, marking PR ready, removing the lock.
- `.operator/scripts/handoff-audit.py 96` passes after this `docs(slice)` follow-up — the audit gates strictly on the `docs(slice)` commit prefix for the handoff commit (initial attempt used `chore(slice)`, which the audit rejected as "missing handoff commit"). Cross-slice convention-check: agent-handoff.md should call out the prefix requirement explicitly so the next adapter slice doesn't repeat the round-trip.

### Known gaps at in-review

- **No native `AsyncFakeMusubiClient`.** Adapter ships its own one-file `_AsyncFake` shim wrapping `FakeMusubiClient`; cross-slice ticket above tracks promoting it into `musubi.sdk.testing`. Once that lands, this adapter and the next two (MCP, OpenClaw) drop their local shims.
- **Artifact upload routes through `memories.capture` placeholder.** The SDK's `artifacts.upload` endpoint isn't shipped yet (the SDK has `artifacts.get` + `artifacts.blob` only — write-side moves through the canonical API in slice-api-v0-write). Tests use a `client._upload_handler` injection seam to validate retry + queue semantics; once `artifacts.upload` ships in the SDK, swap the placeholder for the real call (one-line change in `_upload_transcript_with_retry`).
- **Latency budget unverified.** The spec's 200ms p95 budget for fast-path retrieval is asserted by the integration suite (bullet 17), which is out-of-scope. Unit tests verify the surface is correct; latency is the contract-test layer's job.

## Cross-slice tickets opened by this slice

- [[_inbox/cross-slice/slice-adapter-livekit-slice-sdk-py-async-fake|slice-adapter-livekit-slice-sdk-py-async-fake]] — add `AsyncFakeMusubiClient` to `musubi.sdk.testing`; lets adapters drop the local `_AsyncFake` shim.

## PR links

- [#96](https://github.com/ericmey/musubi/pull/96) — `slice/slice-adapter-livekit` → `v2`.
