---
title: "Slice: reconcile 1Password Connect deployment source"
slice_id: slice-deploy-1password-source-reconcile
section: _slices
type: slice
status: done
owner: codex-gpt5
phase: "SEC-005 containment"
tags: [section/slices, status/done, type/slice, security, p0, deploy]
updated: 2026-07-14
reviewed: true
issue: 423
depends-on: []
blocks: []
---

# Slice: reconcile 1Password Connect deployment source

Production partially reflects unmerged commit `db4f554` from
`feat/musubi-1password-connect`, while current `main` still owns the older
persistent-secret Ansible templates. The deployed unit renders a Qdrant scrape
token to `/run/musubi-secrets`, but the deployed Compose file and running
Prometheus container mount `/etc/musubi/qdrant.token`.

## Scope

`owns_paths`:

- `deploy/ansible/**`
- `tests/ops/test_1password_connect_deploy.py`
- `tests/ops/test_ansible.py`
- `tests/ops/test_update_playbook.py`
- `tests/ops/test_prometheus.py`
- `docs/Musubi/_slices/slice-deploy-1password-source-reconcile.md`
- `docs/Musubi/_inbox/locks/slice-deploy-1password-source-reconcile.lock`

`forbidden_paths`:

- `src/musubi/**`
- `.github/**`
- all retrieval, API, SDK, adapter, and lifecycle tests

The old branch is evidence, not a merge target. Transcribe its intended design
onto current `main` and preserve every newer deployment change.

This slice is a prerequisite for the separately governed SEC-005 credential
rotation, but that rotation is not a Musubi slice-DAG node and is therefore not
listed in `blocks`.

## Specs to implement

- [[_slices/slice-deploy-1password-source-reconcile]] — issue #423 owns the
  source/deployment reconciliation and the Test Contract below.

## Test Contract

1. `test_systemd_renders_qdrant_token_to_runtime_directory_before_compose`
2. `test_prometheus_mounts_rendered_runtime_qdrant_token_read_only`
3. `test_material_musubi_secrets_are_not_rendered_to_persistent_files`
4. `test_config_play_renders_op_reference_templates_and_restarts_on_change`
5. `test_deploy_play_uses_runtime_secret_templates`
6. `test_op_connect_inputs_are_root_only_and_secret_tasks_are_no_log`
7. `test_ansible_templates_remain_parseable_controls`
8. `test_update_preserves_prometheus_bind_inode_and_verifies_lifecycle_target`
9. `test_lifecycle_worker_metrics_survive_source_reconciliation`
10. `test_update_asserts_recreated_core_services_match_the_pinned_digest`
11. `test_ansible_op_run_tasks_source_the_root_only_connect_environment`
12. `test_deploy_preserves_prometheus_bind_inode_and_verifies_lifecycle_target`

## Work log

- 2026-07-13, codex-gpt5: Claimed issue #423 after live, value-free proof of
  source/deployment drift. First commit is the red contract; no deployment or
  credential rotation is authorized by this slice.
- 2026-07-13, codex-gpt5: Expanded ownership to the existing deployment
  contract tests whose persistent-secret/module assumptions change with the
  accepted 1Password runtime model. Targeted update semantics and Prometheus
  authentication remain asserted through the `op run`/tmpfs boundary.
- 2026-07-13, codex-gpt5: Source candidate `581ad9c` preserves phased deploy
  ordering and per-service updates while moving runtime secret resolution to
  the systemd/1Password boundary. Local gate: 52 passed, 1 pre-existing skip;
  four Ansible syntax checks, ruff, strict mypy, and rendered Compose parsing
  clean. Status remains `in-progress`: independent review, check-mode diff,
  serial deployment, and runtime proof are still open; SEC-005 rotation remains
  forbidden.
- 2026-07-13, codex-gpt5: Independent cross-repo review found that the candidate
  omitted the production-proven Prometheus bind-inode and lifecycle-target
  verification from the operational `hw-ansible` source at `8e4da88`. Added a
  strict red before changing source. Issue #423 and `hw-ansible` issue #3 stay
  open until both repositories converge and the actual laptop operator path is
  check-mode reviewed.
- 2026-07-13, codex-gpt5: The source-to-source diff also proved the candidate
  removed the deployed lifecycle-worker metrics endpoint, healthcheck, and
  Prometheus scrape job. Added a separate strict red before restoring those
  operationally proven blocks; an internally green candidate is not sufficient
  if it regresses the actual operator source.
- 2026-07-13, codex-gpt5: The operational diff also found that the candidate
  removed the post-recreate container-image assertion used to prevent a silent
  stale or downgraded Core/lifecycle worker. Added a third preservation red;
  credential-source cleanup may not delete the signed-image convergence gate.
- 2026-07-13, codex-gpt5: Pre-check-mode review found the candidate's Ansible
  `op run` commands only prove `/etc/musubi/connect.env` exists; unlike systemd,
  they never load it, so Connect authentication variables cannot reach `op`.
  Added a strict red requiring value-safe export from the root-only file before
  either repository is applied.
- 2026-07-13, codex-gpt5: Tama's independent cross-repo comparison confirmed
  update/template parity and found the same bind-inode/reload/target-verification
  contract missing from re-runnable `deploy.yml`. Added a deploy-specific strict
  red; first deploy may no-op the reload, but re-deploy must be observably safe.
- 2026-07-14, Yua: Closed after the reconciled source shipped in v1.13.4 and a
  normal full-stack restart proved the signed Core/lifecycle digest, healthy
  dependencies, 8/8 Prometheus targets, the tmpfs-only Qdrant scrape-token
  mount, removal of the legacy persistent token, and value-free parity between
  the live Qdrant key and 1Password item version 5. Issue #423 is closed.
