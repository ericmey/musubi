# Musubi Backup and Restore

This directory contains the first backup/restore scaffold for the Musubi host.

## Stores

- Qdrant collections snapshot every six hours and copy to `/mnt/snapshots/qdrant/<timestamp>/`.
- The vault pushes to its private git remote every 15 minutes; rsync to warm snapshots is the fallback.
- `lifecycle-work.sqlite` and cursor files copy hourly into `/mnt/snapshots/sqlite/` and `/mnt/snapshots/cursors/`.
- Artifact blobs rsync hourly with `--delete-after`; content-addressed paths make repeats idempotent.
- Warm snapshots can be pushed to encrypted offsite storage with restic and Backblaze B2 credentials from Ansible Vault.

## Playbooks

- `backup.yml` performs on-demand or scheduled backups.
- `restore.yml` restores filesystem stores, recovers Qdrant snapshots, rebuilds curated vectors, and verifies artifact chunk counts.
- `drill.yml` composes bootstrap + restore + smoke validation for quarterly restore drills.

## Credentials

Keep real values in Ansible Vault, not git. The expected variable names are documented in `deploy/ansible/vault.example.yml`.

## RPO and RTO

Software-level restore targets roughly 30 minutes once a host is already provisioned. Hardware-level disaster recovery is bounded by the Ansible bootstrap plus data restore time.
