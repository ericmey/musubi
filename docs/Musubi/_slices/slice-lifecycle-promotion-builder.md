---
title: "Slice: Wire real promotion jobs into the lifecycle runner"
slice_id: slice-lifecycle-promotion-builder
section: _slices
type: slice
status: done
owner: claude-code-opus-4-7
phase: "6 Lifecycle"
tags: [section/slices, status/done, type/slice, lifecycle, promotion, runner]
updated: 2026-04-21
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-promotion]]", "[[_slices/slice-vault-sync]]"]
blocks: []
---

# Slice: Wire real promotion jobs into the lifecycle runner

> Replace the placeholder-lambda at job name `promotion` in the
> running lifecycle worker with a real `build_promotion_jobs()` helper
> that composes `run_promotion_sweep` against live `PromotionLLM`,
> `VaultWriter`, and `ThoughtEmitter` implementations.

**Phase:** 6 Lifecycle · **Status:** `in-review` · **Owner:** `claude-code-opus-4-7`

## Why this slice exists

The promotion sweep is the most dependency-heavy of the four unbuilt
sweeps:

- `PromotionLLM` Protocol needs an httpx-backed Ollama call that
  renders a concept as human-readable markdown.
- `VaultWriter` Protocol needs a real file writer that respects the
  vault path + write-log echo filter — this depends on the vault-sync
  machinery that `slice-vault-sync` shipped. There's a VaultWriter
  class somewhere we can lift.
- `ThoughtEmitter` Protocol needs an adapter over `ThoughtsPlane.send`
  (the Protocol signature is `emit(channel, content, title)`; the
  plane's signature is `send(thought)` — adapter is 10 lines).

## Specs to implement

- [[06-ingestion/lifecycle-engine]] §Job registry
- [[06-ingestion/promotion]] (the sweep's spec)
- [[13-decisions/0025-lifecycle-runner-without-apscheduler]]

## Owned paths (you MAY write here)

- `src/musubi/llm/promotion_client.py` (new — httpx `PromotionLLM`
  impl + prompt file at `src/musubi/llm/prompts/promotion-render/v1.txt`).
- `tests/llm/test_promotion_client.py` (new).

### Coordination notes (paths NOT claimed — shared with sibling slices)

- A tiny `ThoughtEmitter` adapter over `ThoughtsPlane.send` is needed
  but shared with demotion + reflection builders. First builder-slice
  to land creates it; later ones import.
- The existing `slice-lifecycle-promotion` owns `lifecycle/promotion.py`;
  this slice contributes an additive `build_promotion_jobs()` helper
  to that file via a `spec-update:` cross-slice pattern.
- The lifecycle runner (owned by this slice-builder family) needs a
  small extension to `build_lifecycle_jobs()` + its test; coordinate
  with whichever sibling slice ships first on the signature shape.

## Forbidden paths

- `src/musubi/vault/` — existing vault-sync code is frozen here;
  reuse rather than modify. Open a cross-slice ticket if a
  `VaultWriter` refactor is genuinely needed.
- `src/musubi/lifecycle/maturation.py`.
- `src/musubi/lifecycle/scheduler.py`.

## Definition of Done

![[00-index/definition-of-done]]

Plus:

- [ ] `python -m musubi.lifecycle.runner` dispatches `promotion` at
      `04:00` UTC and an end-to-end promotion lands a real markdown
      file under `/var/lib/musubi/vault/<ns>/concepts/`.
- [ ] A thought lands in the ops channel when promotion succeeds.
- [ ] Rollback: setting the `PromotionLLM` to the `_NotConfigured`
      stub causes the job to no-op gracefully (log warn, not crash).

## Work log

### 2026-04-21 — claude-code-opus-4-7

- `src/musubi/llm/prompts/promotion-render/v1.txt` + `src/musubi/llm/promotion_client.py`:
  `HttpxPromotionClient` satisfying `PromotionLLM`. Raises on failure (no `None`-return) so the
  sweep's existing try/except records a specific rejection reason.
- `src/musubi/lifecycle/promotion.py`: added `build_promotion_jobs()` helper wired to
  `run_promotion_sweep` behind `file_lock("promotion.lock")` at cron `04:00` UTC.
- `src/musubi/lifecycle/runner.py`: `build_lifecycle_jobs()` grew a `promotion_jobs=` kwarg;
  `_main_async()` composes the real `VaultWriter` (from `src/musubi/vault/writer.py`) + the
  `HttpxPromotionClient` + the shared `ThoughtsPlaneEmitter` into the production
  `PromotionDeps`.
- `tests/llm/test_promotion_client.py` (new): 8 tests covering happy path, network / 5xx,
  invalid envelope, and every validation branch (no H2, AI-disclaimer, body too short).
- `tests/lifecycle/test_runner.py`: two new wiring tests
  (`_wires_promotion_builders`, `_merges_all_four_builder_groups`).

**Local verification:** `make check` → 1134 passed / 231 skipped. Commits follow
tests-first ordering this time (per reviewer feedback on #165).

## PR links

- PR — to be opened after local push.
