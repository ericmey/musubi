#!/usr/bin/env bash
# Host-local backup driver for Musubi.
#
# Runs on the Musubi host (not from Ansible). A systemd timer invokes this
# every six hours. The driver snapshots every canonical store to a local
# directory; offsite replication (restic to Backblaze B2) is a separate
# concern handled by `deploy/backup/backup.yml` when its vault vars land.
#
# Why a shell script and not the existing `backup.yml` ansible playbook:
#
# - Self-contained. If yua (ansible control host) is down, musubi still
#   backs itself up. The recovery path should not depend on a second host.
# - Compose-aware. The ansible playbook targets Qdrant on 127.0.0.1:6333,
#   which doesn't exist in the compose-era (Qdrant is bound to the
#   `musubi_default` bridge only). This script shells into the qdrant
#   container's local network via the shared docker daemon.
# - No vault password needed at runtime. Secrets are sourced from
#   `.env.production` (already readable on the host) rather than
#   `ansible-vault decrypt` of `vault.yml`.
#
# On-disk layout produced:
#
#   /var/lib/musubi/backups/<TIMESTAMP>/
#     qdrant/
#       <collection>.snapshot  (file pulled from the qdrant volume)
#       SHA256SUMS
#     sqlite/
#       work.sqlite            (sqlite3 .backup from lifecycle volume)
#     artifact-blobs/           (rsync mirror, content-addressed so ~idempotent)
#     manifest.json            (metadata: epoch, script version, qdrant coll list)
#
# Retention: entries older than RETENTION_DAYS (default 14) are deleted
# after a successful run. Failed runs do NOT rotate — the backup directory
# survives until the next green run or the operator cleans it up.
#
# Exit codes:
#   0   success (all steps)
#   1   precondition failed (compose not up, missing env, etc.)
#   2   one or more snapshot steps failed
#   3   retention-prune failed (non-fatal but surfaced)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MUSUBI_HOME="${MUSUBI_HOME:-/etc/musubi}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/lib/musubi/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
ENV_FILE="${ENV_FILE:-${MUSUBI_HOME}/.env.production}"
COMPOSE_FILE="${COMPOSE_FILE:-${MUSUBI_HOME}/docker-compose.yml}"

# Discover collections dynamically via the Qdrant API at run time. A
# hardcoded list drifts every time the store layout changes (e.g. the
# artifact collection was renamed `musubi_artifact_heads` → `musubi_artifact`
# post-spec). Dynamic discovery keeps the backup self-healing.
# The list is populated in the "Discover collections" step below, after
# we have API-key access validated.
declare -a QDRANT_COLLECTIONS=()

LOCK_FILE="${LOCK_FILE:-/var/lock/musubi-backup.lock}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
  # Single-line JSON so journald-to-prometheus is straightforward later.
  printf '{"ts":"%s","level":"%s","msg":%q}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2"
}

die() {
  log ERROR "$1"
  exit "${2:-1}"
}

require() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

require docker
require rsync
require sha256sum
require jq

[[ -r "$ENV_FILE" ]] || die "env file not readable: $ENV_FILE"
[[ -r "$COMPOSE_FILE" ]] || die "compose file not readable: $COMPOSE_FILE"

# Pull QDRANT_API_KEY out of the env file without exec'ing it. Strip any
# surrounding single- or double-quotes that shell-safe env files add.
raw_key="$(grep -E '^QDRANT_API_KEY=' "$ENV_FILE" | head -n 1 | cut -d= -f2-)"
QDRANT_API_KEY="${raw_key%\"}"
QDRANT_API_KEY="${QDRANT_API_KEY#\"}"
QDRANT_API_KEY="${QDRANT_API_KEY%\'}"
QDRANT_API_KEY="${QDRANT_API_KEY#\'}"
[[ -n "$QDRANT_API_KEY" ]] || die "QDRANT_API_KEY not found in $ENV_FILE"
export QDRANT_API_KEY

# Single-runner guard — prevents overlapping timers from corrupting a snapshot.
exec 9>"$LOCK_FILE"
flock -n 9 || die "another musubi-backup is already running"

# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_ROOT}/${TIMESTAMP}"
mkdir -p "${DEST}/qdrant" "${DEST}/sqlite" "${DEST}/artifact-blobs"

log INFO "backup-starting dest=${DEST}"
STATUS=0

# --- Discover collections --------------------------------------------------

# One round-trip to Qdrant to enumerate. If this fails, every collection
# snapshot below would fail too — bail early with a clear error.
discovered="$(
  docker compose -f "$COMPOSE_FILE" exec -T lifecycle-worker \
    python -c "
import os, sys, httpx, json
r = httpx.get('http://qdrant:6333/collections',
              headers={'api-key': os.environ['QDRANT_API_KEY']}, timeout=15.0)
if r.status_code != 200:
    print('FAIL', r.status_code, r.text[:200], file=sys.stderr); sys.exit(1)
for c in r.json().get('result', {}).get('collections', []):
    print(c.get('name', ''))
" 2>&1
)" || {
  log ERROR "qdrant-collection-discovery-failed out=${discovered}"
  die "cannot enumerate Qdrant collections" 2
}

while IFS= read -r line; do
  name="$(echo "${line}" | tr -d '[:space:]')"
  [[ -n "${name}" ]] && QDRANT_COLLECTIONS+=("${name}")
done <<< "${discovered}"

if [[ ${#QDRANT_COLLECTIONS[@]} -eq 0 ]]; then
  log WARNING "qdrant-no-collections-present"
fi
log INFO "qdrant-collections-discovered count=${#QDRANT_COLLECTIONS[@]}"

# --- Qdrant snapshots ------------------------------------------------------

for coll in "${QDRANT_COLLECTIONS[@]}"; do
  # Trigger a snapshot via the qdrant container's own HTTP API. We exec
  # through the qdrant container's curl — wait, curl isn't in the qdrant
  # image. Use the host's curl against qdrant's exposed port in the
  # compose network by shelling into a container that has curl, or run
  # docker exec with wget… simpler: use the ollama container (minimal
  # but has wget via busybox? no, also minimal). Use `docker run --rm
  # --network musubi_default curlimages/curl` — but that pulls a new
  # image every run. Use a one-shot python from the lifecycle-worker
  # container which we know has httpx.
  result="$(
    docker compose -f "$COMPOSE_FILE" exec -T lifecycle-worker \
      python -c "
import os, sys
import httpx
api = os.environ['QDRANT_API_KEY']
r = httpx.post(
    'http://qdrant:6333/collections/${coll}/snapshots',
    headers={'api-key': api},
    timeout=60.0,
)
if r.status_code != 200:
    print('FAIL', r.status_code, r.text[:200], file=sys.stderr)
    sys.exit(1)
data = r.json().get('result', {})
print(data.get('name', ''))
" 2>&1
  )" || {
    log ERROR "qdrant-snapshot-api-failed coll=${coll} out=${result}"
    STATUS=2
    continue
  }

  snapshot_name="$(echo "${result}" | tail -n 1 | tr -d '[:space:]')"
  if [[ -z "${snapshot_name}" ]]; then
    log ERROR "qdrant-snapshot-empty-name coll=${coll}"
    STATUS=2
    continue
  fi

  src_file="/var/lib/musubi/qdrant-snapshots/${coll}/${snapshot_name}"
  if [[ ! -r "${src_file}" ]]; then
    log ERROR "qdrant-snapshot-file-missing coll=${coll} path=${src_file}"
    STATUS=2
    continue
  fi

  cp -p "${src_file}" "${DEST}/qdrant/${coll}.snapshot"
  log INFO "qdrant-snapshot-ok coll=${coll} bytes=$(stat -c%s "${DEST}/qdrant/${coll}.snapshot")"
done

# Checksum everything we pulled.
if find "${DEST}/qdrant" -type f -name '*.snapshot' -print -quit | grep -q .; then
  (cd "${DEST}/qdrant" && sha256sum *.snapshot > SHA256SUMS)
  log INFO "qdrant-checksums-written"
fi

# --- SQLite (lifecycle ledger + cursors) -----------------------------------

SQLITE_SRC="/var/lib/musubi/lifecycle/work.sqlite"
if [[ -r "${SQLITE_SRC}" ]]; then
  sqlite3_via_docker() {
    docker compose -f "$COMPOSE_FILE" exec -T lifecycle-worker \
      python -c "
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
" "$1" "$2"
  }

  # The sqlite file is a bind-mount visible inside the lifecycle-worker
  # container at the same path. Run the .backup there so we get a
  # consistent copy even if the scheduler is writing.
  if sqlite3_via_docker "${SQLITE_SRC}" "/var/lib/musubi/lifecycle/work.sqlite.backup-${TIMESTAMP}"; then
    # Move the backup file out of the live directory into our snapshot tree.
    mv "/var/lib/musubi/lifecycle/work.sqlite.backup-${TIMESTAMP}" \
       "${DEST}/sqlite/work.sqlite"
    log INFO "sqlite-backup-ok bytes=$(stat -c%s "${DEST}/sqlite/work.sqlite")"
  else
    log ERROR "sqlite-backup-failed"
    STATUS=2
  fi
else
  log WARNING "sqlite-source-missing path=${SQLITE_SRC}"
fi

# --- Artifact blobs --------------------------------------------------------

if [[ -d /var/lib/musubi/artifact-blobs ]]; then
  if rsync -a --delete-after \
       /var/lib/musubi/artifact-blobs/ \
       "${DEST}/artifact-blobs/"; then
    blob_count=$(find "${DEST}/artifact-blobs" -type f | wc -l)
    log INFO "artifact-blobs-rsync-ok count=${blob_count}"
  else
    log ERROR "artifact-blobs-rsync-failed"
    STATUS=2
  fi
fi

# --- Manifest --------------------------------------------------------------

cat > "${DEST}/manifest.json" <<EOF
{
  "schema_version": 1,
  "timestamp": "${TIMESTAMP}",
  "epoch": $(date -u +%s),
  "hostname": "$(hostname)",
  "script_sha256": "$(sha256sum "$0" | cut -d' ' -f1)",
  "status": ${STATUS},
  "qdrant_collections": [$(printf '"%s",' "${QDRANT_COLLECTIONS[@]}" | sed 's/,$//')]
}
EOF

# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

if [[ ${STATUS} -eq 0 ]]; then
  # Only prune if this run was clean — otherwise keep every backup until an
  # operator confirms the failure is understood.
  if find "${BACKUP_ROOT}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" \
       -exec rm -rf {} + 2>/dev/null; then
    log INFO "retention-prune-ok days=${RETENTION_DAYS}"
  else
    log WARNING "retention-prune-failed"
    STATUS=3
  fi
fi

log INFO "backup-complete status=${STATUS}"
exit ${STATUS}
