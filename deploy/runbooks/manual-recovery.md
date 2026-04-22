# Manual Musubi recovery from a backup snapshot

Use this when `deploy/backup/restore.yml` cannot run (rot tracked in #190)
or you want a low-cost, low-risk recovery path that doesn't depend on
Ansible at all.

Every step runs directly on `musubi.mey.house` (or wherever Musubi is
deployed). No control host needed.

**When this is your procedure:**
- Perf testing corrupted state and you want to roll back to a known-good.
- An incident took out a Qdrant collection and you just need that one back.
- You're exercising the safety net before putting the system under load.

---

## Pre-flight

**Command:**

```bash
# Identify the snapshot you want to restore from.
sudo ls -la /var/lib/musubi/backups/
# Pick one — typically the most recent scheduled snapshot or a labeled
# baseline like `pre-perf-baseline-2026-04-22`.
SNAP=/var/lib/musubi/backups/pre-perf-baseline-2026-04-22
sudo cat "$SNAP/manifest.json" | jq .
# Verify SHA256SUMS — files must match what was recorded.
cd "$SNAP/qdrant" && sudo sha256sum -c SHA256SUMS
```

**Expected output:** `manifest.json` shows `status: 0` and a list of
seven collections under `qdrant_collections`. `sha256sum -c` shows every
`*.snapshot` as `OK`.

**Destructive:** no.

**Rollback:** not applicable (check).

---

## 1. Stop the stack

**Command:**

```bash
sudo docker compose -f /etc/musubi/docker-compose.yml ps
sudo docker compose -f /etc/musubi/docker-compose.yml stop core lifecycle-worker
```

**Expected output:** `core` and `lifecycle-worker` go from `running` to
`exited` or `stopped`. Qdrant, TEI, Ollama keep running — they're the
targets of the restore.

**Destructive:** yes — stops serving traffic. Consumers see connection
refused until step 6 restarts the stack.

**Rollback:** `sudo docker compose -f /etc/musubi/docker-compose.yml start core lifecycle-worker`
restarts without restoring. Use if you decide to abort the recovery.

---

## 2. Restore Qdrant collections from snapshot files

Qdrant's snapshot files are uploaded back via its HTTP API. API key
lives in `/etc/musubi/.env.production` as `QDRANT_API_KEY=`.

**Command:**

```bash
QDRANT_API_KEY=$(sudo grep '^QDRANT_API_KEY=' /etc/musubi/.env.production \
  | cut -d= -f2- | tr -d '"'"'"'')
QDRANT_URL=http://127.0.0.1:6333   # or the network-internal host:port

# List existing collections (pre-restore inventory).
curl -sf -H "api-key: $QDRANT_API_KEY" "$QDRANT_URL/collections" | jq .

# For each collection snapshot, upload it back. Qdrant's recover-from-snapshot
# flavour accepts the file over multipart upload.
for snap in "$SNAP"/qdrant/*.snapshot; do
  coll=$(basename "$snap" .snapshot)
  echo "=> restoring $coll"
  # Delete the existing collection first (destructive) — the upload
  # endpoint is snapshot-recovery, not snapshot-merge.
  curl -sf -X DELETE -H "api-key: $QDRANT_API_KEY" \
    "$QDRANT_URL/collections/$coll" | jq -c .
  # Upload + recover.
  curl -sf -X POST \
    -H "api-key: $QDRANT_API_KEY" \
    -F "snapshot=@$snap" \
    "$QDRANT_URL/collections/$coll/snapshots/upload?priority=snapshot" \
    | jq -c .
done

# Verify the collection count matches the manifest.
curl -sf -H "api-key: $QDRANT_API_KEY" "$QDRANT_URL/collections" \
  | jq '.result.collections | length'
```

**Expected output:** seven collections, each showing
`"status":"ok","result":true` on upload. Final count = 7.

**Destructive:** yes — pre-existing collections of the same name are
deleted before the upload. Nothing survives the re-import.

**Rollback:** there is no rollback inside this step. If the upload
fails mid-way, re-run step 2 from a different snapshot, or fall back
to the snapshot created by the 6-hour timer right before the attempt.

---

## 3. Restore SQLite lifecycle ledger

**Command:**

```bash
# Backup the current sqlite aside, in case you need to diff later.
sudo cp -a /var/lib/musubi/lifecycle-work.sqlite \
  /var/lib/musubi/lifecycle-work.sqlite.pre-restore.$(date -u +%s) || true

sudo cp -a "$SNAP/sqlite/work.sqlite" /var/lib/musubi/lifecycle-work.sqlite
sudo chown musubi:musubi /var/lib/musubi/lifecycle-work.sqlite
sudo chmod 0640 /var/lib/musubi/lifecycle-work.sqlite
```

**Expected output:** no output on success (the `cp` is quiet).

**Destructive:** yes — overwrites the current lifecycle-work SQLite.
The `.pre-restore.<epoch>` copy is your immediate undo.

**Rollback:** `sudo cp -a /var/lib/musubi/lifecycle-work.sqlite.pre-restore.<epoch> /var/lib/musubi/lifecycle-work.sqlite`
restores the pre-recovery state.

---

## 4. Restore artifact blobs

**Command:**

```bash
# rsync -a --delete will remove any blobs present on the host but not
# in the snapshot — that's correct for a full recovery, matches the
# snapshot's intent. Drop --delete if you want additive recovery.
sudo rsync -a --delete \
  "$SNAP/artifact-blobs/" \
  /var/lib/musubi/artifact-blobs/
sudo chown -R musubi:musubi /var/lib/musubi/artifact-blobs/
```

**Expected output:** no output (rsync quiet by default). Size should
match `du -sh "$SNAP/artifact-blobs"`.

**Destructive:** yes — `--delete` removes host blobs not in the snapshot.

**Rollback:** if you kept a pre-restore copy (recommended for safety),
`rsync -a --delete /var/lib/musubi/artifact-blobs.pre-restore/ /var/lib/musubi/artifact-blobs/`.
Without one, rolling back means restoring from a different snapshot.

---

## 5. Vault is always restored from git

The Obsidian vault is the store-of-record for curated knowledge and
is backed by the remote git repo (see `slice-vault-sync`). No local
restore step is required; if the vault working copy was damaged, nuke
it and re-clone:

```bash
sudo rm -rf /var/lib/musubi/vault
sudo -u musubi git clone <vault-remote> /var/lib/musubi/vault
```

The curated Qdrant collection is regenerated from vault content on
the next vault-sync tick after the stack is back up. No explicit
rebuild command needed.

---

## 6. Restart the stack

**Command:**

```bash
sudo docker compose -f /etc/musubi/docker-compose.yml up -d core lifecycle-worker
# Wait for healthchecks.
for i in $(seq 30); do
  state=$(sudo docker inspect musubi-core-1 -f '{{.State.Health.Status}}')
  echo "[$i] core health: $state"
  [ "$state" = "healthy" ] && break
  sleep 2
done
```

**Expected output:** `core health: healthy` within ~30s. If it hangs
at `starting` for over a minute, check `sudo docker logs musubi-core-1
--tail 50` for a bootstrap failure.

**Destructive:** no.

**Rollback:** `sudo docker compose -f /etc/musubi/docker-compose.yml stop`.

---

## 7. Smoke-verify

**Command:**

```bash
# Public health.
curl -sf http://127.0.0.1:8100/v1/ops/health | jq .

# Per-plane row counts — confirm each collection restored its population.
for coll in musubi_episodic musubi_curated musubi_concept musubi_thought \
            musubi_artifact musubi_artifact_chunks musubi_lifecycle_events; do
  count=$(curl -sf -H "api-key: $QDRANT_API_KEY" \
    "$QDRANT_URL/collections/$coll" | jq -r '.result.points_count // "n/a"')
  echo "$coll: $count"
done

# One round-trip through the canonical API (operator token required;
# see .agent-context.local.md for how to mint one).
```

**Expected output:** `{"status":"ok","version":"v0"}` from health.
Each collection reports its points_count; compare against any
historical baseline you trust.

**Destructive:** no.

**Rollback:** not applicable (verification).

---

## Scope this runbook does NOT cover

- **Multi-host restore.** Musubi is single-node per ADR-0010.
- **Partial-collection restore.** Today the snapshot is all-or-nothing
  per collection. Restoring only some rows from a Qdrant snapshot
  requires manual point enumeration and is out of scope here.
- **Disaster recovery to a fresh host.** The `bootstrap.yml` Ansible
  playbook installs host-level deps + creates users + configures UFW
  — follow that first, THEN this runbook for the data recovery half.
  Once #190 lands, `drill.yml` will automate the combined flow.

## When the automated `restore.yml` is repaired (#190)

Prefer the automated playbook:

```bash
ansible-playbook -i deploy/ansible/inventory.yml \
  -e @~/.musubi-secrets/inventory-vars.yml \
  -e @~/.musubi-secrets/vault.yml \
  -e 'backup_timestamp=pre-perf-baseline-2026-04-22' \
  deploy/backup/restore.yml
```

This manual runbook stays in-repo as the escape hatch.
