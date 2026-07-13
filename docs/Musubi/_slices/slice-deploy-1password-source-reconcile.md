---
title: "Slice: reconcile 1Password Connect deployment source"
slice_id: slice-deploy-1password-source-reconcile
section: _slices
type: slice
status: in-progress
owner: codex-gpt5
phase: "SEC-005 containment"
tags: [section/slices, status/in-progress, type/slice, security, p0, deploy]
updated: 2026-07-13
reviewed: false
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
