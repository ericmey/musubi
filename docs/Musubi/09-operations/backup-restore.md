---
title: "Backup & Restore"
section: 09-operations
tags: [backup, disaster-recovery, operations, restore, section/operations, status/draft, type/runbook]
type: runbook
status: draft
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: false
---
# Backup & Restore

How we back up, where copies live, and how we restore. Practiced quarterly.

## Scope

From [[09-operations/asset-matrix]], four stores need backup:

1. **Vault** (Markdown + frontmatter) → git.
2. **Qdrant** (episodic, concepts, curated mirror, artifact heads/chunks, thoughts) → snapshot API.
3. **Artifact blobs** → rsync.
4. **sqlite** (lifecycle-work.sqlite) → `sqlite3 .backup`.

Everything else (config, secrets) is in Ansible's repo / 1Password — different lifecycle.

## Storage tiers

| Tier | Medium | Retention |
|---|---|---|
| Hot | Primary NVMe (live) | n/a |
| Warm | SATA SSD (`/mnt/snapshots`) | 90 days |
| Cold | Off-box (NAS or S3) | 1 year |
| Git | GitHub private repos | indefinite |

## Vault

### Strategy: git

The vault is a git repo. Cron commits + pushes every 15 min if changes exist:

```bash
#!/usr/bin/env bash
# /opt/musubi/git-sync.sh
cd /var/lib/musubi/vault
git add -A
if git diff --cached --quiet; then
  exit 0
fi
git commit -m "autosync $(date -Iseconds)"
git push --quiet origin main
```

Credentials: deploy key managed by Ansible. The operator's Obsidian working vault is a live sync target; in v1 the Musubi host restores from the architecture vault bundled in this monorepo under `docs/Musubi/` (see [[13-decisions/0016-vault-in-monorepo]]) plus any operator-owned curated notes synced via Syncthing.

### Restore

```bash
cd /var/lib/musubi
mv vault vault.bak.<ts>                                  # keep the old copy aside
git clone git@github.com:<operator>/<musubi-repo>.git    # the monorepo
ln -s musubi/docs/Musubi vault                      # or rsync; whichever the Ansible role prefers
chown -R musubi:musubi vault
systemctl restart musubi
musubi-cli index rebuild --collection musubi_curated --source vault
```

The CLI's rebuild re-encodes all curated docs. Qdrant's curated collection is now in sync.

### RPO

15 minutes — the cron interval. Lower RPO is possible (shorter interval or real-time watcher pushing) but unnecessary for v1 scope.

## Qdrant

### Strategy: snapshot API

Every 6 hours:

```bash
# /opt/musubi/qdrant-snapshot.sh
TS=$(date +%Y%m%dT%H%M%S)
curl -X POST \
  -H "api-key: $QDRANT_API_KEY" \
  http://localhost:6333/snapshots
# Qdrant writes to /var/lib/musubi/qdrant/snapshots/musubi-<ts>.snapshot (full)
# Also per-collection if we want; full is simpler.

rsync -a /var/lib/musubi/qdrant/snapshots/ \
         /mnt/snapshots/qdrant/$TS/
find /mnt/snapshots/qdrant/ -mindepth 1 -maxdepth 1 -type d -mtime +90 -exec rm -rf {} \;
```

Snapshots are consistent — Qdrant flushes WAL and tarballs the collection directory.

### Restore (full)

```bash
systemctl stop musubi
# Wipe Qdrant storage or rename it:
mv /var/lib/musubi/qdrant/collections /var/lib/musubi/qdrant/collections.bak.<ts>
# Copy snapshot files back:
cp /mnt/snapshots/qdrant/<ts>/* /var/lib/musubi/qdrant/snapshots/
# Recover:
systemctl start musubi
musubi-cli qdrant recover-from-snapshot --name musubi-<ts>
```

Alternative (Qdrant REST):

```bash
curl -X PUT \
  -H "api-key: $QDRANT_API_KEY" \
  http://localhost:6333/collections/musubi_episodic/snapshots/recover \
  -d '{"location": "file:///qdrant/storage/snapshots/musubi_episodic-<ts>.snapshot"}'
```

### Restore (per-collection)

Useful if only one collection is corrupt:

```bash
# Wipe just that collection:
curl -X DELETE -H "api-key: ..." http://localhost:6333/collections/musubi_episodic
# Recreate from snapshot:
curl -X PUT -H "api-key: ..." \
  http://localhost:6333/collections/musubi_episodic/snapshots/recover \
  -d '{"location": "file:///qdrant/storage/snapshots/musubi_episodic-<ts>.snapshot"}'
```

### RPO

6 hours. If we lose more than 6h of episodic + concept data, that's the gap. Acceptable for v1; if we want tighter, increase snapshot cadence (runs in < 60s at v1 scale).

### RTO

~5 minutes for a full restore at v1 scale.

## Artifact blobs

### Strategy: rsync

Hourly cron:

```bash
rsync -a --delete-after \
  /var/lib/musubi/artifact-blobs/ \
  /mnt/snapshots/artifact-blobs/
```

Cheap — most blobs don't change. `--delete-after` removes snapshots of blobs that were purged (rare operator action).

### Cold tier

Optional nightly rsync of `/mnt/snapshots/artifact-blobs/` to a NAS or S3 bucket. Configurable; off by default.

### Restore

```bash
rsync -a /mnt/snapshots/artifact-blobs/ /var/lib/musubi/artifact-blobs/
```

That's it. Core re-reads on next lookup.

## sqlite

### Strategy: sqlite3 `.backup`

Daily:

```bash
sqlite3 /var/lib/musubi/lifecycle-work.sqlite \
  ".backup /mnt/snapshots/sqlite/lifecycle-$(date +%F).sqlite"
find /mnt/snapshots/sqlite/ -name "*.sqlite" -mtime +30 -delete
```

`.backup` is safe on a live DB (uses WAL).

### Restore

```bash
systemctl stop musubi
cp /mnt/snapshots/sqlite/lifecycle-<ts>.sqlite \
   /var/lib/musubi/lifecycle-work.sqlite
systemctl start musubi
```

Most tables are restartable (cursors, locks). `lifecycle_events` is append-only history — losing recent events means recent state transitions aren't auditable, but current state is intact in Qdrant/vault.

### RPO

24 hours.

## Off-site

Optional. If enabled in Ansible, a nightly `restic` backup pushes `/mnt/snapshots/` to:

- A Synology NAS on the LAN, OR
- Backblaze B2 / AWS S3, OR
- A friend's offsite server.

Encrypted with a key held in 1Password.

Retention: 1 year of daily; 5 years of monthly.

This is the only off-site copy. Untested → not trusted → quarterly restore drill.

## Restore drills

Quarterly. Ansible ships a `drill.yml` playbook that:

1. Spins up a disposable Docker host (`musubi-drill`).
2. Installs Musubi from scratch.
3. Restores vault, Qdrant snapshot, artifact blobs, sqlite from the latest backups.
4. Runs the smoke suite ([[07-interfaces/contract-tests#smoke]]).
5. Reports: did restore work, how long did it take, did smoke pass?

Output is archived. If it fails, that's the most important ops issue to address before anything else.

## Corruption detection

Before trusting a backup, validate:

- **Vault:** `git fsck --full` weekly.
- **Qdrant snapshot:** snapshot file has a checksum manifest; `sha256sum` matches what Qdrant wrote.
- **Artifact blobs:** content-addressed — filename IS the hash. `sha256sum` on a sample row confirms integrity.
- **sqlite:** `PRAGMA integrity_check;` weekly.

Failed check → alert fires; don't overwrite the last-known-good snapshot.

## Full-disaster recovery

Scenario: host is totaled. Everything on /var/lib/musubi gone.

Steps:

1. Provision fresh Ubuntu box → run Ansible bring-up.
2. Pull latest off-site `restic` backup → restore `/mnt/snapshots/` first.
3. Restore vault from GitHub: `git clone …`.
4. Restore artifact blobs: rsync from `/mnt/snapshots/artifact-blobs/` to `/var/lib/musubi/`.
5. Start Musubi stack; Qdrant empty.
6. `musubi-cli qdrant recover-from-snapshot --name <latest>`.
7. Run smoke suite. Verify.
8. Re-issue tokens if the JWT signing key was lost (force all clients to re-auth).

Time: ~1 hour assuming backups are warm. Slower if off-site is S3 over a slow link.

## What we do NOT back up

- **/tmp, /var/cache** — ephemeral.
- **TEI model cache** — re-downloads on first use.
- **Ollama model weights** — re-downloads on first use.
- **Access logs > 30 days** — rotated off; not critical.

## Test contract

**Module under test:** backup scripts + drill playbook

1. `test_git_sync_commits_only_when_changed`
2. `test_qdrant_snapshot_creates_file_and_rsyncs`
3. `test_artifact_rsync_delete_after_removes_purged_blobs`
4. `test_sqlite_backup_completes_under_5s_at_v1_scale`
5. `test_drill_playbook_restores_to_working_musubi`
6. `test_restore_drill_smoke_suite_passes_within_5min`
7. `test_corruption_check_fails_on_tampered_snapshot` (chaos)
