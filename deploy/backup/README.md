# Musubi Backup and Restore

This directory contains two complementary backup paths for the Musubi host.

## 1. Host-local scheduled backups (live today)

[`musubi-backup.sh`](musubi-backup.sh) runs under [`systemd/musubi-backup.timer`](systemd/musubi-backup.timer)
every six hours on `musubi.example.local`. It is self-contained — no Ansible,
no secondary host, no vault password at run time. Recovery does not depend
on the ansible control host being up.

Install + enable (one-shot, from the host):

```bash
sudo install -m 0755 deploy/backup/musubi-backup.sh /usr/local/bin/musubi-backup
sudo install -m 0644 deploy/backup/systemd/musubi-backup.service /etc/systemd/system/
sudo install -m 0644 deploy/backup/systemd/musubi-backup.timer /etc/systemd/system/
sudo install -d -o root -g root -m 0755 /var/lib/musubi/backups
sudo systemctl daemon-reload
sudo systemctl enable --now musubi-backup.timer
```

Run once on demand:

```bash
sudo systemctl start musubi-backup.service
journalctl -u musubi-backup.service -n 50
```

Per-run layout (`/var/lib/musubi/backups/<TIMESTAMP>/`):

```
qdrant/<collection>.snapshot + SHA256SUMS
sqlite/work.sqlite (lifecycle ledger)
artifact-blobs/ (content-addressed rsync)
manifest.json (status, epoch, collection list)
```

Retention: 14 days, pruned only after a green run so we never lose the
last-known-good backup to a half-failed next one.

### Verifying a backup

```bash
# Integrity: checksums match every snapshot file in the dir
sudo sha256sum -c /var/lib/musubi/backups/<TIMESTAMP>/qdrant/SHA256SUMS

# Manifest says green
sudo jq .status /var/lib/musubi/backups/<TIMESTAMP>/manifest.json # → 0
```

A `status` of `0` means every store backed up; `2` means at least one
Qdrant collection or sqlite step failed and retention was held open
pending operator review.

## 2. Ansible-driven full backup (kept for drills + offsite push)

## Stores

- Qdrant collections snapshot every six hours and copy to `/var/lib/musubi/backups/<timestamp>/qdrant/` (host-local, live). The ansible path below additionally copies to `/mnt/snapshots/qdrant/` when that tier exists.
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
