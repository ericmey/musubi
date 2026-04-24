---
title: "Slice: Capture endpoint"
slice_id: slice-ingestion-capture
section: _slices
type: slice
status: done
owner: vscode-cc-sonnet47
phase: "1 Schema"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-types]]", "[[_slices/slice-plane-episodic]]", "[[_slices/slice-api-v0-write]]"]
blocks: ["[[_slices/slice-adapter-mcp]]", "[[_slices/slice-adapter-livekit]]"]
---

# Slice: Capture endpoint

> Sync write path. Dedup at ingestion similarity threshold. Provisional state; async enrichment downstream.

**Phase:** 1 Schema · **Status:** `done` · **Owner:** `vscode-cc-sonnet47`

## Specs to implement

- [[06-ingestion/capture]]

## Owned paths (you MAY write here)

- `src/musubi/ingestion/capture.py`
- `tests/ingestion/test_capture.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/lifecycle/`
- `src/musubi/planes/`
- `src/musubi/api/`
- `src/musubi/retrieve/`
- `src/musubi/types/`

## Depends on

- [[_slices/slice-types]]
- [[_slices/slice-plane-episodic]]
- [[_slices/slice-api-v0-write]]

Start this slice only after every upstream slice has `status: done`.

## Unblocks

- [[_slices/slice-adapter-mcp]]
- [[_slices/slice-adapter-livekit]]

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

- Claimed slice atomically via `gh issue edit 10 --add-assignee @me`. Issue #10, PR #86 (draft).
- Branch `slice/slice-ingestion-capture` off `v2`.
- Caught the same `owns_paths` `src/`-prefix drift Hana flagged on `slice-retrieval-blended`; operator landed reconcile PR #83 (commit `870bc84`) before this claim — claim made against canonical state.

### 2026-04-19 — vscode-cc-sonnet47 — handoff to in-review

- Landed `src/musubi/ingestion/{__init__,capture}.py`: `CaptureService` + `IngestionIdempotencyCache` (sqlite-backed, per-(token_jti, namespace, key)) + `DEFAULT_DEDUP_THRESHOLDS` (per-plane similarity config) + bounded retry (`TimeoutError`-only, 3 attempts, exponential backoff) around `EpisodicPlane.create`. Pydantic models for `CaptureRequest` / `CaptureResult` / `CaptureError` covering the spec § Contract field set with content-length validation 1..16000.
- Tests: 20 passing + 7 skipped-with-reason for the spec's 22 Test Contract bullets, plus 4 coverage tests. **Coverage on `src/musubi/ingestion/capture.py` is 99 % branch** (gate 85 %).
- Three cross-slice tickets opened against type/plane gaps the spec calls for but the current types/plane shape rejects.
- Handoff checks: `make check` 708 passed / 177 skipped clean, `make tc-coverage SLICE=slice-ingestion-capture` exits 0 (Closure Rule satisfied), `make agent-check` clean (no `^  ✗` errors; only pre-existing `⚠` warnings + drift on two parallel agents' slices), `gh pr view 86 --json mergeStateStatus` is `CLEAN` + `mergeable=MERGEABLE`, `gh pr checks 86` reports both checks pass remotely, PR body first line is `Closes #10.`, `git ls-files` shows 7 owned files all present + the `feat(ingestion)` commit at `1f39554` touches them.

#### Architectural notes for the reviewer

- **Two parallel implementations.** The HTTP `POST /v1/episodic` endpoint (slice-api-v0-write, `src/musubi/api/routers/writes_episodic.py::capture`) ships its own dedup + idempotency middleware; this slice ships the canonical `CaptureService` per the spec's design. Today they coexist with **uncoordinated** dedup semantics — the API endpoint is fine for HTTP callers; the service is the right entry point for non-HTTP callers (CLI ingestion, batch loaders, other adapters). A follow-up cross-slice will rewire `writes_episodic.capture` to delegate to `CaptureService`; that PR would touch `src/musubi/api/` which is in this slice's `forbidden_paths`. Open it as a chore PR after merge.
- **Per-token idempotency via sqlite, not in-memory.** The HTTP middleware's cache is process-local + dict-based; this service's cache is sqlite-backed because the spec calls for 24h TTL + `(token, namespace, key)` keying — durable across worker restarts and bearer-distinguished. Tests use `expire_for_test(...)` to exercise the TTL path without sleeping 24h.
- **Dedup detection via post-create version check.** `EpisodicPlane.create` returns `version=1` on a fresh insert, `version>1` on a reinforce-merge (the `_reinforce` helper bumps version inside). The service uses this as the `dedup_action="merged"` signal — no extra Qdrant probe needed. Cleanly avoids the spec/plane gap on "longer-content-wins" (deferred to a cross-slice ticket).
- **Retry budget is `TimeoutError`-only.** `ConnectionError` / `OSError` (TEI down) propagate immediately as `Err(BACKEND_UNAVAILABLE)` — retry won't help when the upstream is unreachable. Only transient timeouts get the 3-attempt budget.
- **Three cross-slice tickets opened**, all against plane / type gaps the spec calls for but the current shape rejects:
  - `slice-types-capture-event-record.md` — `LifecycleEvent` validator rejects the spec's `provisional → provisional` capture-event emit (bullet 5).
  - `slice-plane-episodic-merge-strategy.md` — `EpisodicPlane._reinforce` always-replaces; spec wants longer-content-wins (bullet 10).
  - `slice-plane-episodic-batch-create.md` — needed for the spec's single-TEI + single-Qdrant batch path (bullets 20-21).

#### Test Contract coverage matrix

| # | Bullet | State | Where |
|---|---|---|---|
| 1 | `test_capture_returns_202_and_object_id` | ✓ passing | `tests/ingestion/test_capture.py` |
| 2 | `test_capture_writes_provisional_state` | ✓ passing | `tests/ingestion/test_capture.py` |
| 3 | `test_capture_writes_both_vectors` | ✓ passing | `tests/ingestion/test_capture.py` |
| 4 | `test_capture_sets_timestamps_server_side` | ✓ passing | `tests/ingestion/test_capture.py` |
| 5 | `test_capture_emits_lifecycle_event` | ⏭ skipped | deferred → `slice-types-capture-event-record` (LifecycleEvent rejects provisional→provisional) |
| 6 | `test_capture_p95_under_250ms_on_100k_corpus` | ⊘ out-of-scope | benchmark — needs real Qdrant + TEI; deferred to future `slice-perf-bench` |
| 7 | `test_dedup_merges_on_high_similarity` | ✓ passing | `tests/ingestion/test_capture.py` |
| 8 | `test_dedup_increments_reinforcement_count` | ✓ passing | `tests/ingestion/test_capture.py` |
| 9 | `test_dedup_merges_tag_union` | ✓ passing | `tests/ingestion/test_capture.py` |
| 10 | `test_dedup_keeps_longer_content` | ⏭ skipped | deferred → `slice-plane-episodic-merge-strategy` |
| 11 | `test_dedup_disabled_on_curated` | ✓ passing | `tests/ingestion/test_capture.py` (config-level test against `DEFAULT_DEDUP_THRESHOLDS` + `is_dedup_enabled`) |
| 12 | `test_idempotency_key_returns_same_object_twice` | ✓ passing | `tests/ingestion/test_capture.py` |
| 13 | `test_idempotency_key_expires_after_24h` | ✓ passing | `tests/ingestion/test_capture.py` (uses `expire_for_test`) |
| 14 | `test_idempotency_key_scoped_per_token` | ✓ passing | `tests/ingestion/test_capture.py` |
| 15 | `test_capture_empty_content_returns_400` | ✓ passing | `tests/ingestion/test_capture.py` (pydantic `min_length=1` validator) |
| 16 | `test_capture_forbidden_namespace_returns_403` | ⏭ skipped | deferred → `src/musubi/api/auth.py` (HTTP layer); already tested in `tests/api/test_api_v0_write.py::test_capture_rejects_out_of_scope_namespace` |
| 17 | `test_capture_tei_down_returns_503` | ✓ passing | `tests/ingestion/test_capture.py` |
| 18 | `test_capture_qdrant_retry_logic_succeeds_on_transient_failure` | ✓ passing | `tests/ingestion/test_capture.py` |
| 19 | `test_capture_qdrant_permanent_failure_returns_503` | ✓ passing | `tests/ingestion/test_capture.py` |
| 20 | `test_batch_capture_single_tei_embed_call` | ⏭ skipped | deferred → `slice-plane-episodic-batch-create` |
| 21 | `test_batch_capture_single_qdrant_upsert` | ⏭ skipped | deferred → `slice-plane-episodic-batch-create` |
| 22 | `test_batch_capture_100_items_under_1s` | ⊘ out-of-scope | benchmark — same as bullet 6 |

## Cross-slice tickets opened by this slice

- [`_inbox/cross-slice/slice-ingestion-capture-slice-types-capture-event-record.md`](../_inbox/cross-slice/slice-ingestion-capture-slice-types-capture-event-record.md) — `LifecycleEvent` rejects the spec's `provisional → provisional` capture-event emit (bullet 5).
- [`_inbox/cross-slice/slice-ingestion-capture-slice-plane-episodic-merge-strategy.md`](../_inbox/cross-slice/slice-ingestion-capture-slice-plane-episodic-merge-strategy.md) — `EpisodicPlane._reinforce` always-replaces; spec wants longer-content-wins (bullet 10).
- [`_inbox/cross-slice/slice-ingestion-capture-slice-plane-episodic-batch-create.md`](../_inbox/cross-slice/slice-ingestion-capture-slice-plane-episodic-batch-create.md) — needed for the spec's single-TEI + single-Qdrant batch path (bullets 20-21).

## PR links

- #86 — `feat(ingestion): slice-ingestion-capture` (in-review)
