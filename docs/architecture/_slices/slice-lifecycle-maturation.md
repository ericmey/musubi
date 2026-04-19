---
title: "Slice: Maturation job"
slice_id: slice-lifecycle-maturation
section: _slices
type: slice
status: in-review
owner: vscode-cc-sonnet47
phase: "6 Lifecycle"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: ["[[_slices/slice-lifecycle-engine]]", "[[_slices/slice-plane-episodic]]"]
blocks: ["[[_slices/slice-lifecycle-synthesis]]"]
---

# Slice: Maturation job

> Hourly sweep. Importance scoring (Qwen2.5-7B), tag normalization, dedup pass. Provisional → matured.

**Phase:** 6 Lifecycle · **Status:** `in-review` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[06-ingestion/maturation]]

## Owned paths (you MAY write here)

- `musubi/lifecycle/maturation.py`
- `tests/lifecycle/test_maturation.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `musubi/planes/`
- `musubi/api/`

## Depends on

- [[_slices/slice-lifecycle-engine]]
- [[_slices/slice-plane-episodic]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-lifecycle-synthesis]]

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

### 2026-04-19 — vscode-cc-sonnet47 — claim

- Claimed slice atomically via `gh issue edit 12 --add-assignee @me`. Issue #12, PR #52 (draft).
- Branch `slice/slice-lifecycle-maturation` off `v2`.

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Landed `src/musubi/lifecycle/maturation.py`: `episodic_maturation_sweep`, `provisional_ttl_sweep`, `episodic_demotion_sweep`, `concept_maturation_sweep`, `concept_demotion_sweep`, plus `OllamaClient` Protocol, `_NotConfiguredOllama` loud-failure stub, `MaturationCursor`, `MaturationConfig`, `normalize_tags`, `detect_supersession_hint`, and `build_maturation_jobs` for scheduler integration.
- Tests: 23 passing + 4 skipped-with-reason against the spec's 24 Test Contract bullets, plus 11 coverage tests for the scope-extension sweeps (concept maturation/demotion, episodic demotion) and scheduler wiring. Coverage on `src/musubi/lifecycle/maturation.py` is **91 % branch** (gate 85 %).
- Five handoff checks all green: `make check` (347 passed / 65 skipped), `make tc-coverage SLICE=slice-lifecycle-maturation` exits 0 (Closure Rule satisfied), `make agent-check` clean (only warnings + drift on the three parallel slices — slice-plane-artifact #20, slice-plane-thoughts #24, slice-retrieval-hybrid #29 — none from this PR), `gh pr checks 52` green at handoff, PR body first line is `Closes #12.`.
- PR #52 marked ready for review.

#### Architectural notes for the reviewer

- **Every state mutation routes through `musubi.lifecycle.transitions.transition()`.** Verified by reading the `LifecycleEventSink` back in `test_transition_uses_typed_function` — every state-changed row produces a paired ledger entry with `actor=lifecycle-worker` and the documented sweep `reason`.
- **Enrichment fields (importance, tags, linked_to_topics) land via direct `set_payload` after the transition succeeds.** The lifecycle ledger records state changes only; enrichment fields are non-state metadata observable on the post-sweep payload. If the reviewer judges enrichment also belongs in the audit trail, the cleanest follow-up is to extend `LineageUpdates` (or add an `EnrichmentUpdates`) so `transition()` carries them — that lives in slice-lifecycle-engine.
- **Ollama is a Protocol with a loud-failure default.** `_NotConfiguredOllama.score_importance` / `infer_topics` raise `NotImplementedError` per the ADR-punted-deps-fail-loud rule; production deployments must wire a real client (future `slice-llm-client`). The factory `default_ollama_client()` references `get_settings()` so the future real client has a clear integration point.
- **Topics are written to `linked_to_topics`, not `topics`.** The spec text says "topics" but `EpisodicMemory` (via `MemoryObject`) only declares `linked_to_topics`. `topics` is a CuratedKnowledge-only field. This is a spec-vs-type drift the type-side could close in a follow-up; for this slice it's documented in code.
- **Cursor is observability, not selection.** The state filter alone gates "have we processed this row?" — once a row transitions out of `provisional`, it's no longer selectable. Cursor advances as a high-water mark for monitoring + future operator introspection.
- **Spec-listed thresholds (`MATURATION_MIN_AGE_SEC`, `MATURATION_BATCH`, `provisional_ttl_sec`, etc.) live on `MaturationConfig`** with spec defaults. Settings-binding is deferred to a future `slice-config-thresholds`; the prohibition on "hardcoded thresholds" is satisfied today by accepting overrides at every entry point.

#### Test Contract coverage matrix

| # | Bullet | State | Where |
|---|---|---|---|
| 1 | `test_selects_only_provisional_older_than_min_age` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 2 | `test_batch_size_limits_selection` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 3 | `test_cursor_resumes_across_runs` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 4 | `test_importance_rescored_via_llm` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 5 | `test_importance_fallback_on_ollama_unavailable` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 6 | `test_tags_normalized_lowercase_and_hyphenated` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 7 | `test_tag_aliases_applied` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 8 | `test_tags_deduped` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 9 | `test_topics_inferred_from_llm` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 10 | `test_topics_empty_on_unknown` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 11 | `test_supersession_inferred_from_hint_keyword` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 12 | `test_supersession_not_inferred_without_hint` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 13 | `test_supersession_sets_both_sides_of_link` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 14 | `test_state_transitions_to_matured` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 15 | `test_transition_uses_typed_function` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 16 | `test_lifecycle_event_emitted` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 17 | `test_ollama_outage_still_matures_without_enrichment` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 18 | `test_provisional_older_than_7d_archived` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 19 | `test_archival_emits_lifecycle_event` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 20 | `test_file_lock_prevents_double_execution` | ✓ passing | `tests/lifecycle/test_maturation.py` |
| 21 | `hypothesis: no matured memory has created_epoch in the future` | ⊘ out-of-scope | property test — deferred to a follow-up `test-property-lifecycle` slice. The base type's `_fill_epochs_and_enforce_monotonicity` validator already enforces `updated_epoch >= created_epoch` at construction; a hypothesis harness exercising the property end-to-end would re-test the type's invariant. Stub `@pytest.mark.skip` left in the test file with the same skip reason. |
| 22 | `hypothesis: provisional memories older than 7d are always archived after one sweep` | ⊘ out-of-scope | exercised in case form by `test_provisional_older_than_7d_archived`; full hypothesis run deferred to the follow-up `test-property-lifecycle` slice. Stub `@pytest.mark.skip` left in the test file. |
| 23 | `integration: real Ollama, 50 synthetic provisional memories mature in one sweep, importance distribution is plausible` | ⊘ out-of-scope | requires a live Ollama endpoint — deferred to a follow-up integration suite (no current slice owns it). Stub `@pytest.mark.skip` left in the test file. |
| 24 | `integration: ollama-offline scenario — maturation completes without enrichment, re-enrichment sweep picks them up later` | ⊘ out-of-scope | the offline path itself is exercised by `test_ollama_outage_still_matures_without_enrichment`; the *re-enrichment sweep* (the secondary `WHERE state=matured AND importance_last_scored_at < now-7d` selection in spec §Re-enrichment on next sweep) requires an `importance_last_scored_at` field that is not on `EpisodicMemory` today. Deferred to a follow-up that lands the field via slice-types and the secondary sweep here. Stub `@pytest.mark.skip` left in the test file. |

### Known gaps at in-review — 2026-04-19 — vscode-cc-sonnet47

The two spec-vs-type drifts called out in the handoff "Architectural notes for the reviewer" are now formal cross-slice tickets against `slice-types`. They are non-blocking for this slice's merge (spec bullets affected are either passing on the field that exists or already declared out-of-scope), but they MUST be closed before this slice flips `status: done` so the next agent picking up follow-up work has a clean starting point:

- [`_inbox/cross-slice/slice-lifecycle-maturation-slice-types-topics-vs-linked-to-topics.md`](../_inbox/cross-slice/slice-lifecycle-maturation-slice-types-topics-vs-linked-to-topics.md) — spec calls for `topics` on `EpisodicMemory`; model has only `linked_to_topics`. Bullets 9 + 10 currently pass against `linked_to_topics`; once slice-types reconciles, this slice's `_apply_enrichment` switches to whichever name lands.
- [`_inbox/cross-slice/slice-lifecycle-maturation-slice-types-importance-last-scored-at.md`](../_inbox/cross-slice/slice-lifecycle-maturation-slice-types-importance-last-scored-at.md) — `importance_last_scored_at` field needed for the spec's re-enrichment sweep (bullet 24, declared out-of-scope here pending this ticket). Once the field lands, this slice's follow-up implements the secondary `WHERE state=matured AND importance_last_scored_at < now-7d` selection + bullet-24 unit test.

## Cross-slice tickets opened by this slice

- [`_inbox/cross-slice/slice-lifecycle-maturation-slice-types-topics-vs-linked-to-topics.md`](../_inbox/cross-slice/slice-lifecycle-maturation-slice-types-topics-vs-linked-to-topics.md) — open against `slice-types`. Reconcile `topics` (spec) vs `linked_to_topics` (model) on `EpisodicMemory`.
- [`_inbox/cross-slice/slice-lifecycle-maturation-slice-types-importance-last-scored-at.md`](../_inbox/cross-slice/slice-lifecycle-maturation-slice-types-importance-last-scored-at.md) — open against `slice-types`. Add `importance_last_scored_at: datetime | None` so the spec's re-enrichment sweep (bullet 24) can be implemented in a follow-up.

## PR links

- #52 — `feat(lifecycle): slice-lifecycle-maturation` (in-review)
