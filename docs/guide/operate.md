# Operate it

## Health

- `GET /v1/ops/health`: is Core up.
- `GET /v1/ops/status`: per-dependency status (Qdrant, the TEI services,
  Ollama), for dashboards and smoke checks.
- `GET /v1/ops/metrics`: Prometheus metrics. The stack's local Prometheus
  scrapes it; point `remote_write` at your own long-term store if you have
  one.

## Upgrades

1. **Find the new digest** on the release's GitHub page, and verify it with
   `cosign verify` as in [Install](install.md#run-it-for-real-one-host).
2. **Read the release notes** in [CHANGELOG.md](../../CHANGELOG.md).
3. **Pin the new digest** in your Compose file, then pull it and recreate:

   ```bash
   docker compose --env-file .env.production pull
   docker compose --env-file .env.production up -d
   curl -fsS http://127.0.0.1:8100/v1/ops/health
   ```

4. **Check your agents** before calling the upgrade done: a real capture and
   recall from each integration you run, not only a health check.
5. **Roll back** by pinning the previous digest and repeating step 3.

Ansible-managed hosts have a fuller procedure, with a credential preflight
that runs the new image against your live tokens before it goes live:
[upgrade runbook](../../deploy/runbooks/upgrade.md) and
[image upgrade](../../deploy/runbooks/upgrade-image.md).

## Backups

[`deploy/backup/`](../../deploy/backup/README.md) has a self-contained
host-local job: a systemd timer that, every six hours, snapshots each Qdrant
collection and copies the lifecycle database and the artifact blobs into
`/var/lib/musubi/backups/<timestamp>/`, keeping 14 days.

**That job does not cover the vault.** The curated plane is Markdown in the
vault directory; keep it in git and push it to a private remote on a
schedule (the backup README describes this). Also copy the backup directory
off the host. A restore needs all three: Qdrant snapshots, the lifecycle
database and the vault.

Test a restore before you need one:
[backup and restore](../Musubi/09-operations/backup-restore.md) and the
[manual recovery runbook](../../deploy/runbooks/manual-recovery.md).

## Alerts

[Alerts](../Musubi/09-operations/alerts.md) lists the conditions worth paging
on, and which are dashboard-only. Wire them into whatever alerting you
already run.
