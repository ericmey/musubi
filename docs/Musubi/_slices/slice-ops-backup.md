---
title: "Slice: Backup / restore"
slice_id: slice-ops-backup
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/done, type/slice]
updated: 2026-04-19
reviewed: true
depends-on: ["[[_slices/slice-ops-ansible]]"]
blocks: ["[[_slices/slice-ops-first-deploy]]"]
---

# Slice: Backup / restore

> Nightly snapshot to local NAS + S3-compatible offsite + weekly restore-into-scratch drill.

**Phase:** 8 Ops · **Status:** `done` · **Owner:** `codex-gpt5`

## Specs to implement

- [[09-operations/backup-restore]]
- [[09-operations/asset-matrix]]

## Owned paths (you MAY write here)

  - `deploy/backup/`
  - `musubi/ops/backup.py`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/planes/`

## Depends on

  - [[_slices/slice-ops-ansible]]

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

### 2026-04-19 19:18 — codex-gpt5 — claimed slice

- Claimed Issue #17 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-19 19:25 — codex-gpt5 — handoff to in-review

- Added backup, restore, and restore-drill playbooks under `deploy/backup/`, plus git-sync/restic templates and an operator README.
- Added lightweight backup helper code at `src/musubi/ops/backup.py` for deterministic checksum and SQLite backup behavior.
- Extended the Ansible scaffold with `ansible.posix` and vault example placeholders needed by the backup playbooks.
- Verification: `make check` passed; `make tc-coverage SLICE=slice-ops-backup` passed; `make agent-check` reported warnings only and no `✗` hard errors.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_git_sync_commits_only_when_changed` | ✓ passing | `tests/ops/test_backup.py:35` |
| `test_qdrant_snapshot_creates_file_and_rsyncs` | ✓ passing | `tests/ops/test_backup.py:43` |
| `test_artifact_rsync_delete_after_removes_purged_blobs` | ✓ passing | `tests/ops/test_backup.py:53` |
| `test_sqlite_backup_completes_under_5s_at_v1_scale` | ✓ passing | `tests/ops/test_backup.py:61` |
| `test_drill_playbook_restores_to_working_musubi` | ✓ passing | `tests/ops/test_backup.py:76` |
| `test_restore_drill_smoke_suite_passes_within_5min` | ✓ passing | `tests/ops/test_backup.py:85` |
| `test_corruption_check_fails_on_tampered_snapshot` | ✓ passing | `tests/ops/test_backup.py:99` |
| `test_every_asset_has_canonical_owner_documented` | ✓ passing | `tests/ops/test_backup.py:109` |
| `test_backup_cadence_matches_claimed_rpo` | ✓ passing | `tests/ops/test_backup.py:127` |
| `test_restore_drills_run_quarterly` | ✓ passing | `tests/ops/test_backup.py:136` |
| `test_curated_rebuild_from_vault_produces_matching_qdrant_count` | ✓ passing | `tests/ops/test_backup.py:142` |
| `test_artifact_rechunk_produces_same_chunk_count_as_snapshot` | ✓ passing | `tests/ops/test_backup.py:150` |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- [PR #82](https://github.com/ericmey/musubi/pull/82)
