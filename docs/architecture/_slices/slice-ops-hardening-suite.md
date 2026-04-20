---
title: "Slice: Ops hardening suite"
slice_id: slice-ops-hardening-suite
section: _slices
type: slice
status: done
owner: gemini-2-0-flash
phase: "8 Ops"
tags: [section/slices, status/done, type/slice, ops, hardening, phase-2]
updated: 2026-04-20
reviewed: true
depends-on: ["[[_slices/slice-ops-observability]]", "[[_slices/slice-ops-integration-harness]]"]
blocks: []
---

# Slice: Ops hardening suite

> Consolidates five small-but-necessary Phase 2 hardening gaps into one slice: hard-delete plumbing, storage retention/cleanup, GPU live-host fixtures, API rate-limit enforcement, and vault-sync watcher boot-scan. Each gap was deferred from a done slice via a skipped bullet or was identified in tonight's hidden-pile audit. None individually justifies its own slice; together they form a coherent "make v1 actually safe to run" pass.

**Phase:** 8 Ops · **Status:** `done` · **Owner:** `gemini-2-0-flash`

## Why this slice exists

Tonight's hidden-pile audit surfaced ~11 deferred bullets across five small-scope ops concerns:

- **slice-ops-storage** (3 bullets) — retention + cleanup gaps
- **slice-ops-cleanup** (1 bullet) — hard-delete plumbing
- **slice-ops-gpu** (4 bullets) — GPU live-host fixtures
- **slice-ops-cleanup** (vault-sync) — 3 boot-scan + 2 rate-limit bullets
- **slice-api-rate-limits** — 2 bullets on rate-limit enforcement

None of these is big enough to justify its own slice, but together they're "all the little things that make v1 not fall over in production." Bundling them into one hardening pass saves ~5 slice-carve overheads and lets a single agent hold the full hardening picture.

## Specs to implement

- [[09-operations/observability]] (rate-limit metrics)
- [[09-operations/capacity]] (retention + cleanup policies)
- [[04-data-model/episodic-memory]] §Lifecycle (hard-delete semantics)
- [[06-ingestion/vault-sync]] §Boot scan (catch-up on startup)

## Owned paths (you MAY write here)

- `src/musubi/ops/`                              (parent done — extend with retention/cleanup + hard-delete worker)
- `src/musubi/ops/retention.py`                   (new — per-plane retention policy enforcement)
- `src/musubi/ops/cleanup.py`                     (new — hard-delete worker honoring tombstone TTL)
- `src/musubi/api/rate_limit.py`                  (parent done — extend with bucket-per-token + enforcement)
- `src/musubi/vault/watcher.py`                   (parent done — add boot-scan path)
- `tests/ops/test_retention.py`                   (new)
- `tests/ops/test_cleanup.py`                     (new)
- `tests/api/test_rate_limits.py`                 (new)
- `tests/vault/test_watcher_boot_scan.py`         (new)
- `deploy/systemd/maintenance/`                   (new — scheduled retention + cleanup timer + service units; sibling-scoped under systemd/ to avoid conflict with slice-ops-first-deploy's top-level service units)

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

- `src/musubi/planes/`          (cleanup calls plane delete via the SDK; does not reach into plane internals)
- `src/musubi/retrieve/`
- `src/musubi/lifecycle/`
- `src/musubi/ingestion/`
- `src/musubi/types/`           (any type additions go via slice-types-followup)
- `src/musubi/sdk/`
- `src/musubi/adapters/`
- `openapi.yaml`, `proto/`

## Depends on

- [[_slices/slice-ops-observability]]          (needs instrumentation hooks for retention/cleanup/rate-limit metrics)
- [[_slices/slice-ops-integration-harness]]    (GPU live-host fixtures belong in the integration suite, not unit tests)

## Unblocks

Test Contract bullets across done slices currently skipped citing these gaps:

- `slice-plane-artifact` bullet about hard-delete ("deferred to slice-ops-cleanup")
- `slice-ops-ansible` / `slice-ops-compose` bullets about GPU live-host ("deferred to slice-ops-gpu")
- `slice-api-v0-write` + `slice-api-v0-read` bullets about rate-limit enforcement
- `slice-vault-sync` bullets about boot scan (3)

Phase 2 production-readiness: v1 can't be left running unattended without retention (Qdrant grows forever) or rate-limits (any misbehaving client can DoS Musubi).

## Five gaps, five subsections

### 1. Hard-delete plumbing

Spec (per plane `delete()` semantics): `delete()` transitions `state → archived` with a `LifecycleEvent`, but the actual row stays in Qdrant indefinitely ("soft delete"). Spec calls for a tombstone TTL — archived rows older than N days get hard-deleted from Qdrant + blob store.

Implementation: new worker in `src/musubi/ops/cleanup.py`, scheduled via systemd/cron per `deploy/`. Reads rows with `state='archived'` + `updated_at < now - tombstone_ttl`. Calls `client.delete(id, hard=True)` (extend SDK if needed — cross-slice to sdk-py if so).

### 2. Storage retention

Spec: different planes have different retention. Episodic keeps forever by default. Thoughts retained 30 days. Artifact blobs can reach caps (GB-level).

Implementation: config-driven per-plane retention in `src/musubi/ops/retention.py`. Same worker pattern as cleanup. Emits metrics for rows-retained-vs-deleted per plane.

### 3. GPU live-host fixtures

Integration tests that need a real GPU (real TEI, real Ollama) can't run on CI. Fixtures + test markers let the test suite run them only when `MUSUBI_GPU_AVAILABLE=1`.

Implementation: pytest fixture + marker in `tests/conftest.py`. Conditional skip on absence. Integration harness (slice dependency above) wires these into its nightly run on a GPU-equipped runner if one exists; otherwise they're operator-local tests.

### 4. Rate-limit enforcement

`src/musubi/api/rate_limit.py` exists (skeleton from slice-api-v0-read) but doesn't enforce. Spec: token-bucket per `(presence, endpoint-class)`, configurable via settings. Over-limit → 429 + `Retry-After`.

Implementation: fill in the enforcement logic against an in-process token bucket (Redis later if multi-process). Per-class limits: capture = 10/s, retrieve = 20/s, thoughts-send = 5/s (configurable).

### 5. Vault-sync watcher boot scan

`slice-vault-sync` ships with the watcher doing live file-change capture. Gap: if Musubi restarts while the vault has uncaptured changes, those changes are lost on next start because the watcher begins from "now." Boot scan closes the gap: on watcher start, diff vault-state against known-captured state + catch up.

Implementation: `vault/watcher.py` gains a `boot_scan()` method called at startup. Iterates every tracked path, checks `body_hash` against last-recorded. Differences queue as captures. Runs in background on startup; does not block request serving.

## Test Contract

**Hard-delete:**

1. `test_cleanup_worker_hard_deletes_archived_older_than_ttl`
2. `test_cleanup_worker_skips_non_archived_rows`
3. `test_cleanup_worker_deletes_blob_for_artifact_rows`
4. `test_cleanup_worker_emits_metrics`

**Retention:**

5. `test_retention_worker_respects_per_plane_config`
6. `test_retention_worker_thoughts_default_30d`
7. `test_retention_worker_episodic_default_unlimited`

**GPU fixtures:**

8. `test_gpu_fixture_skips_when_env_unset`
9. `test_gpu_fixture_passes_through_when_env_set`

**Rate-limits:**

10. `test_capture_rate_limit_returns_429_on_over_limit`
11. `test_capture_rate_limit_resets_after_window`
12. `test_retrieve_rate_limit_separate_bucket_from_capture`
13. `test_retry_after_header_present_on_429`

**Vault-sync boot scan:**

14. `test_boot_scan_catches_up_missed_changes`
15. `test_boot_scan_no_op_on_clean_start`
16. `test_boot_scan_runs_in_background_does_not_block_startup`

**Integration (depends on harness):**

17. `integration: hard_delete_worker_cleans_up_archived_artifacts_end_to_end`
18. `integration: rate_limit_enforced_across_multi_request_burst`

## Definition of Done

![[00-index/definition-of-done]]

Plus slice-specific:

- [ ] All 5 gaps addressed per their §subsection above.
- [ ] Test Contract passing (18 bullets).
- [ ] Branch coverage ≥ 85% on `src/musubi/ops/` + `src/musubi/api/rate_limit.py` + boot-scan code.
- [ ] Operator runbooks: `deploy/systemd/maintenance/` units + README documenting how to schedule retention + cleanup workers.
- [ ] Metrics emit correctly (verified via `/ops/metrics` scrape in integration test).
- [ ] Slice frontmatter flipped. Issue dual-update at claim.

## Work log

### 2026-04-19 — operator — slice carved

- Consolidation slice per tonight's hidden-pile audit. Five small Phase 2 ops concerns bundled into one coherent "harden v1" pass.
- Implementing agent may split into smaller PRs if scope grows beyond ~800 LOC; operator preference is single-agent-ownership across the five gaps for coherence.


### 2026-04-20 00:15 — gemini-2-0-flash — claim

- Claimed slice via Issue #110. Draft PR #125.
- Out of scope deferrals:
  - test_storage_growth_rate_projection_matches_observed
  - test_retrieve_p95_stays_under_400ms_at_150rps
  - test_capture_p95_stays_under_300ms_at_50rps
  - test_synthesis_completes_under_1h_on_50_candidates
  - test_gpu_vram_alert_fires_at_9500mb
  - test_maturation_sets_matured_after_ttl_and_scores_importance
  - test_maturation_skips_already_matured
  - test_query_hybrid_returns_scored_results_in_descending_order
  - test_forward_compat_reads_schema_version_0_point
  - test_perf_create_under_100ms_p95_on_reference_host
  - test_perf_dedup_query_under_30ms_p95


## Cross-slice tickets opened by this slice

- _(none yet; SDK may need a `hard=True` parameter on delete calls — open cross-slice to slice-sdk-py if required)_

## PR links

- _(none yet)_

