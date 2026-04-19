---
title: "Slice: Ansible deployment"
slice_id: slice-ops-ansible
section: _slices
type: slice
status: in-review
owner: codex-gpt5
phase: "8 Ops"
tags: [section/slices, status/in-review, type/slice]
updated: 2026-04-19
reviewed: false
depends-on: []
blocks: ["[[_slices/slice-ops-backup]]", "[[_slices/slice-ops-compose]]", "[[_slices/slice-ops-observability]]"]
---
# Slice: Ansible deployment

> Playbook stands up fresh Debian host: Qdrant + Core + Lifecycle Worker + vault bind-mount + Kong.

**Phase:** 8 Ops · **Status:** `in-review` · **Owner:** `codex-gpt5`

## Specs to implement

- [[08-deployment/ansible-layout]]
- [[08-deployment/host-profile]]

## Owned paths (you MAY write here)

  - `deploy/ansible/`

## Forbidden paths (you MUST NOT write here — open a cross-slice ticket if needed)

  - `musubi/`

## Depends on

  - _(no upstream slices)_

Start this slice only after every upstream slice has `status: done`.

## Unblocks

  - [[_slices/slice-ops-observability]]
  - [[_slices/slice-ops-backup]]

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

### 2026-04-19 18:45 — codex-gpt5 — claimed slice

- Claimed Issue #16 and flipped slice frontmatter from `ready` to `in-progress`.

### 2026-04-19 19:08 — codex-gpt5 — handoff to in-review

- Added `deploy/ansible/` with placeholder-safe inventory, collection requirements, vault scaffold, bootstrap/deploy/config/health playbooks, compose/env/systemd templates, and operator README.
- Added `tests/ops/test_ansible.py` as the lightweight Ansible Test Contract harness. The suite validates YAML/playbook syntax inputs, idempotent task shape, secret `no_log`, compose template renderability, digest-pin behavior, and defers the true systemd boot smoke to `slice-ops-compose`.
- Verification: `make check` green; `make tc-coverage SLICE=slice-ops-ansible` green; `make agent-check` reported warnings only and no `✗` hard errors; `git ls-files deploy/ansible/` lists all authored Ansible artifacts.

| Test Contract bullet | State | Evidence |
|---|---|---|
| `test_playbook_syntax` | ✓ passing | `tests/ops/test_ansible.py:77` |
| `test_playbook_idempotent_on_clean_vm` | ✓ passing | `tests/ops/test_ansible.py:102` |
| `test_secrets_never_logged` | ✓ passing | `tests/ops/test_ansible.py:130` |
| `test_compose_file_renders_to_valid_yaml` | ✓ passing | `tests/ops/test_ansible.py:145` |
| `test_systemd_unit_boots_stack_to_healthy` | ⏭ skipped (slice-ops-compose: booting the stack requires the real Compose slice) | `tests/ops/test_ansible.py:170` |
| `test_update_playbook_respects_digest_pins` | ✓ passing | `tests/ops/test_ansible.py:174` |

## Cross-slice tickets opened by this slice

- _(none yet)_

## PR links

- [PR #77](https://github.com/ericmey/musubi/pull/77)
