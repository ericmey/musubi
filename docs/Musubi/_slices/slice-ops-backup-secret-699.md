---
title: "Slice: Backup driver container-secret boundary"
slice_id: slice-ops-backup-secret-699
issue: 699
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/done, type/slice, operations]
updated: 2026-08-14
reviewed: true
depends-on: [slice-ops-backup]
blocks: [slice-ops-backup-compose-warning-706]
---

# Slice: Backup driver container-secret boundary

The production `musubi-backup.timer` remained active while every observed run
failed before `backup-starting`. The installed driver expected
`QDRANT_API_KEY` in persistent `.env.production`, but current deployment
correctly injects material secrets into containers through 1Password Connect.

## Decision boundary

- Keep the host-local timer and driver as the backup execution authority.
- Do not materialize `QDRANT_API_KEY` in a host env file, process argv, or log.
- Execute Qdrant discovery and snapshot requests inside `lifecycle-worker`,
  which already has the key in its container environment.
- Bound the claim to backup execution: Compose still receives the secret in
  host process memory during service startup so it can inject the container.
  This slice removes backup-time host parsing and export; it does not claim the
  secret never transits the host or close a broader host-exposure question.
- Preserve locking, full-store coverage, failure status, checksums, and
  green-only retention semantics.
- Treat `deploy/backup/musubi-backup.sh` in this repository as the canonical
  installed artifact. `hw-ansible` references the artifact but does not carry a
  second copy to update.

## Owned paths

- `deploy/backup/musubi-backup.sh`
- `deploy/backup/README.md`
- `tests/ops/test_backup_scheduler.py`
- `docs/Musubi/_slices/slice-ops-backup-secret-699.md`

## Specs to implement

- [[_slices/slice-ops-backup-secret-699]] — this slice and its `## Test
  Contract` below.

## Test Contract

- `test_script_uses_container_secret_without_host_materialization`
- `test_script_discovers_collections_dynamically`
- `test_script_calls_qdrant_snapshot_api`
- `test_script_retention_only_runs_on_green`

## Work log

- 2026-08-14 — Reproduced the stale host-secret dependency with a red
  structural regression before changing the driver. Production repair remains
  held until the corrected installed script completes a verified green backup.
- 2026-08-14 — Removed host-side key parsing and kept Qdrant requests inside
  the already-secret-bearing lifecycle worker. Backup scheduler tests: 19/19;
  shell parse, Ruff, and Test Contract 4/4 passed. Handoff to independent
  review; production installation and the first green backup remain pending.
