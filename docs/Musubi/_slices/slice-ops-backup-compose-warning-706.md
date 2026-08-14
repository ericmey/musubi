---
title: "Slice: backup discovery output isolation"
slice_id: slice-ops-backup-compose-warning-706
issue: 706
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/done, type/slice, backup]
updated: 2026-08-14
reviewed: true
depends-on: [slice-ops-backup-secret-699]
blocks: []
---

# Slice: backup discovery output isolation

The first production run after #701 proved the secret boundary but exposed a
second failure: `docker compose exec` reparsed the stack file without the
runtime-only secret variables, emitted interpolation warnings, and discovery
captured those warnings through `2>&1`. Five warning lines became fake Qdrant
collection names; the real snapshots completed, but the run failed and its
manifest was invalid JSON.

## Decision boundary

- Resolve exactly one running lifecycle worker by its Compose project and
  service labels.
- Use direct `docker exec` for discovery, snapshot creation, and SQLite backup.
- Fail loudly when zero or multiple matching workers exist.
- Keep secret reads inside the already-running container.
- Do not accept a partial snapshot set or invalid manifest as a backup gate.

## Owned paths

- `deploy/backup/musubi-backup.sh`
- `deploy/backup/README.md`
- `tests/ops/test_backup_scheduler.py`
- `docs/Musubi/_slices/slice-ops-backup-compose-warning-706.md`

## Test Contract

- `test_script_uses_container_secret_without_host_materialization`

## Work log

- 2026-08-14 — Production evidence bound the defect to Compose warning output,
  not Qdrant or the in-container secret. The source test was made red first,
  then rebound to require label resolution plus direct `docker exec` and to
  forbid the Compose CLI in the backup driver.
