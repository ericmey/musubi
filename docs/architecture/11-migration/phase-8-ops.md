---
title: "Phase 8: Ops"
section: 11-migration
tags: [migration, operations, phase-8, section/migration, status/stub, type/migration-phase]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-7-adapters]]"
reviewed: false
---
# Phase 8: Ops

Backups, observability, Ansible-managed config, runbooks, alerts. The "grown-up" phase that makes Musubi operable long-term.

## Goal

A new operator can take over the box with nothing but the Ansible repo, 1Password, and the docs. Nothing essential lives only in the current operator's head.

## Changes

### Ansible-first

Move everything that was done by hand into Ansible. See [[08-deployment/ansible-layout]]. If you SSH to fix something and don't commit that fix to Ansible, the next bring-up will be wrong.

### Snapshot + backup cron

Install the scripts from [[09-operations/backup-restore]]:

- Qdrant 6h snapshot → rsync to SATA.
- Vault 15min git push.
- Artifact blobs hourly rsync.
- Sqlite daily `.backup`.
- Optional nightly restic to off-site.

### Observability stack

Install Prometheus + Grafana + Alertmanager + Loki + OTel Collector. All co-resident on the host. Dashboards provisioned as JSON from the Ansible repo. See [[09-operations/observability]].

### Alerts

Configure per [[09-operations/alerts]]. ntfy push for urgent; email for non-urgent.

### Runbooks

Publish [[09-operations/runbooks]] to the vault. Each alert links to its runbook. Quarterly chaos drills.

### Restore drill

First quarterly drill runs in this phase:

1. `ansible-playbook drill.yml` → provisions disposable host, restores, smoke-tests.
2. Report time + pass/fail.
3. Fix whatever didn't work.

### Capacity dashboards

From [[09-operations/capacity]]. Real numbers after a month of v1 traffic → update projections.

### Documentation

Vault's `docs/` folder contains everything in sections 01-13. Kept under vault-git so changes round-trip.

## Done signal

- Fresh VM → `ansible-playbook musubi.yml` → working host in < 20 min (assuming model weights pre-pulled).
- Dashboards populated and loading.
- All alerts tested via chaos drill.
- Restore drill completes + smoke passes.
- Documentation covers every operational task.

## Rollback

Each component is additive; none of them changes application behavior. Remove one at a time if it misbehaves.

## Smoke test

```
# Bring up a fresh VM (or a known-good existing one):
ansible-playbook playbooks/musubi.yml

# Run the smoke suite:
pytest --contract=smoke --musubi-url=https://musubi.example.local.example.com/v1

# Run a chaos probe:
docker stop musubi-qdrant-1
# Wait 2 min; confirm qdrant_down alert fires; restart; confirm resolved.
```

## Estimate

~2 weeks (mostly Ansible + dashboards; alerts config is straightforward).

## Pitfalls

- **Untested backups.** Snapshot-to-disk isn't a backup until a restore proves it. Drills are mandatory.
- **Alert fatigue.** If alerts fire constantly, they get ignored. Tune thresholds using a week of baseline data before going live on paging.
- **Drift.** "I'll Ansible-ize this later" ends with snowflake boxes. Enforce: commit before it ships.
- **Secret sprawl.** The `.vault.yml` must hold every secret; grep the host for env-like patterns and migrate them.
