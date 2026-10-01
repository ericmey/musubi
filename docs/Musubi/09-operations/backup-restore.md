---
title: "Backup & Restore"
section: 09-operations
tags: [backup, disaster-recovery, operations, restore, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: false
---
# Backup & Restore

How to back up the Compose stack, and how to restore it. The short version is in
`docs/guide/operate.md`; this page adds the detail.

> **Warning: `deploy/backup/restore.yml` does not work, and `deploy/backup/drill.yml`
> (which imports it) fails with it.** It stops `core` and `lifecycle-worker`
> (`restore.yml:100-106`), then runs `docker compose exec -T lifecycle-worker` against the
> stopped container to recover Qdrant (`restore.yml:152-153`), so no snapshot is restored.
> It also stops services before checking that the backup is complete, chooses "latest" by
> directory name without reading the manifest's status, and never checks `SHA256SUMS`.
> Do not rely on these playbooks for recovery. The other files under `deploy/backup/`
> target a different host layout (`/var/lib/musubi` bind mounts), not the named volumes of
> the public stack.

## What to back up

All state lives in six named volumes. Back them up **together, cold, as one set**:

| Volume | Holds |
|---|---|
| `qdrant-storage` | every Qdrant collection: episodic, curated copy, concepts, artifact metadata and chunks, thoughts, lifecycle audit mirror |
| `qdrant-snapshots` | any Qdrant snapshots you have taken |
| `vault` | curated knowledge as Markdown |
| `artifact-blobs` | uploaded artifact bytes |
| `lifecycle` | `work.sqlite` (lifecycle event log, cursors, outbox), `idempotency-receipts.sqlite`, `vault-writelog.db`, job locks |
| `logs` | mounted for `LOG_DIR`; nothing writes to it today, but keep it in the set |

The stores reference each other (Qdrant rows point at vault files and blobs; the lifecycle
outbox points at Qdrant rows), so a set taken at one point in time is the only consistent
backup. Never restore a mix of sets.

**Not backed up:** the GPU overlay's `tei-models` and `ollama-models` volumes are model
caches and download again. Config (`docker-compose.yml`, your `.env`) and secrets belong in
your own config management and secret manager. Losing `JWT_SIGNING_KEY` invalidates every
issued token.

Compose prefixes volume names with the project name, `musubi`, so on the host they are
`musubi_qdrant-storage`, `musubi_vault` and so on (`docker volume ls`).

## Cold backup

1. **Stop the stack:** `docker compose stop` (with the same `-f` files you started with).
2. **Archive every volume** with your usual backup tool, at the same point in time.
3. **Start the stack again:** `docker compose up -d --wait`.

Use the same `-f` files (for example the GPU overlay) on every `docker compose` command
on this page that you used to start the stack.

One way to do step 2 with a throwaway container, from the repo checkout. It stops at the
first failure, and it proves every archive reads back completely before recording
checksums:

```bash
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "backup-$TS"
for v in qdrant-storage qdrant-snapshots vault artifact-blobs lifecycle logs; do
  docker run --rm -v "musubi_$v:/data:ro" -v "$PWD/backup-$TS:/backup" \
    alpine tar -C /data -czf "/backup/$v.tgz" .
  tar -tzf "backup-$TS/$v.tgz" > /dev/null   # fails on a truncated or corrupt archive
done
(cd "backup-$TS" && sha256sum *.tgz > SHA256SUMS)
```

A checksum only proves the file has not changed since it was written; it does not prove
the archive is complete. The `tar -tzf` read-back is what catches a truncated archive.

Then copy the set off the host. Keep as many sets as your retention needs; Musubi does not
prune them. The stack is down for the length of the archive step, so schedule it when
agents are idle.

## Restore

1. **Check the whole set first, before stopping or deleting anything.** All six archives
   must be present, match `SHA256SUMS`, and read back completely. Any failure stops here
   with nothing changed:

   ```bash
   set -euo pipefail
   SET="$PWD/backup-<ts>"
   (cd "$SET" && sha256sum -c SHA256SUMS)
   for v in qdrant-storage qdrant-snapshots vault artifact-blobs lifecycle logs; do
     tar -tzf "$SET/$v.tgz" > /dev/null
   done
   ```

2. **Stop the stack:** `docker compose stop`.
3. **Replace the contents of every volume** from that one set, in the same shell (so
   `set -e` still applies and the loop stops at the first failure):

   ```bash
   for v in qdrant-storage qdrant-snapshots vault artifact-blobs lifecycle logs; do
     docker run --rm -v "musubi_$v:/data" -v "$SET:/backup:ro" \
       alpine sh -ec "find /data -mindepth 1 -delete && tar -C /data -xzf /backup/$v.tgz"
   done
   ```

   If a step fails after the first volume is replaced, do not start the stack: fix the
   cause and run step 3 again for all six volumes from the same set.

4. **Start it:** `docker compose up -d --wait`.
5. **Verify:** `curl -fsS http://127.0.0.1:8100/v1/ops/health`, check
   `GET /v1/ops/status` reports every component healthy, then run the canary from
   [[09-operations/runbooks]] with its dedicated smoke identity.

For a full-disaster recovery on a new host, prepare the host ([[08-deployment/host-profile]]),
clone the repo at the same release, restore your `.env` (including the same
`JWT_SIGNING_KEY` and `QDRANT_API_KEY`), run `docker compose up -d --wait` once so Compose
creates the volumes, then follow the restore steps above. If the signing key was lost, set a
new one and re-mint every agent's token.

## Qdrant snapshots (optional)

A Qdrant snapshot covers only Qdrant. It does not replace the cold set, but it is handy
before an upgrade or to repair one collection. Qdrant publishes no host port, so call its
API from inside the Compose network, for example through the Core container (which has
`curl` and `QDRANT_API_KEY`):

```bash
# Create a snapshot of one collection
docker compose exec core sh -c \
  'curl -fsS -X POST -H "api-key: $QDRANT_API_KEY" http://qdrant:6333/collections/musubi_episodic/snapshots'

# List that collection's snapshots
docker compose exec core sh -c \
  'curl -fsS -H "api-key: $QDRANT_API_KEY" http://qdrant:6333/collections/musubi_episodic/snapshots'
```

Snapshots land under `/qdrant/snapshots/<collection>/` in the `qdrant-snapshots` volume.
To restore one collection from a snapshot already in that volume:

```bash
docker compose exec core sh -c \
  'curl -fsS -X PUT -H "api-key: $QDRANT_API_KEY" -H "Content-Type: application/json" \
     http://qdrant:6333/collections/musubi_episodic/snapshots/recover \
     -d "{\"location\": \"file:///qdrant/snapshots/musubi_episodic/<snapshot-name>\"}"'
```

Restoring a single collection can leave it out of step with the other collections, the
vault and the lifecycle state; prefer the full cold restore unless only that collection is
damaged. A full-storage snapshot (`POST /snapshots`) is restored with Qdrant's own startup
option, not this API; see Qdrant's documentation.

## sqlite (`lifecycle` volume)

The cold set already includes the sqlite files. To take an extra, live-safe copy of
`work.sqlite` (for inspection, or before a risky change), use SQLite's online backup API
from the worker container:

```bash
docker compose exec lifecycle-worker python -c "
import sqlite3
src = sqlite3.connect('/var/lib/musubi/lifecycle/work.sqlite')
dst = sqlite3.connect('/var/lib/musubi/lifecycle/work.copy.sqlite')
src.backup(dst)"
```

A copy taken this way is not consistent with Qdrant at the same moment; do not use it to
restore one store on its own.

## Corruption checks

Before trusting a set:

- **Archives:** `sha256sum -c SHA256SUMS` in the set's directory.
- **sqlite:** after restoring into a scratch stack, run `PRAGMA integrity_check;` on
  `work.sqlite` (for example with `python -c` in the worker container, as above).
- **Artifact blobs:** each artifact's metadata records its blob's SHA-256; spot-check a few
  blobs against it.
- **Vault:** if you also keep it in git, `git fsck --full`.

A failed check means: keep the last known-good set and investigate; never overwrite it.

## Restore drills

Test a restore at least every 90 days (`RESTORE_DRILL_CADENCE_DAYS` in
`src/musubi/ops/backup.py`): restore the latest set into a scratch stack on another
machine or Compose project, check health and status, and run the canary. Record how long
it took. `drill.yml` cannot do this today (see the warning above).

## Test contract

1. `test_restore_drills_run_quarterly` (`tests/ops/test_backup.py`) — the drill cadence
   constant is at most 92 days.
2. `test_sqlite_backup_completes_under_5s_at_v1_scale` (same file) — the online-backup
   helper in `src/musubi/ops/backup.py`.
3. `test_corruption_check_fails_on_tampered_snapshot` (same file) — SHA-256 verification
   rejects a changed file.

Nothing tests the cold-backup procedure on this page end to end.
