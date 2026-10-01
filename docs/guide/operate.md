# Operate it

## Health

- `GET /v1/ops/health`: is Core up.
- `GET /v1/ops/status`: per-dependency status (Qdrant, the TEI services,
  Ollama), for dashboards and smoke checks.
- `GET /v1/ops/metrics`: Prometheus metrics. The stack's local Prometheus
  scrapes it; point `remote_write` at your own long-term store if you have
  one.

## Upgrades

1. **A release publishes a signed image,** and an automatic PR proposes the new
   digests for the Core and lifecycle-worker lines in `docker-compose.yml`.
   That PR never merges itself.
2. **Verify the digest** with `cosign verify`, as in
   [Install](install.md#pin-and-verify-the-image), and read the release notes
   in [CHANGELOG.md](../../CHANGELOG.md).
3. **Run the credential preflight** the PR describes. It starts the candidate
   image against your live tokens and must pass before you upgrade.
4. **Upgrade:** pull the new pin, then `docker compose pull` and
   `docker compose up -d --wait` (with the same `-f` files you started with).
5. **Check your agents** before calling the upgrade done: a real capture and
   recall from each integration you run, not only a health check.
6. **Roll back** by returning `docker-compose.yml` to the previous pin and
   running `docker compose up -d --wait` again.

## Backups

<!-- TODO(compose-first): document the backup job for the Compose stack's
named volumes once deploy/backup supports them. -->

Back up three things: Qdrant snapshots (Qdrant's snapshot API), the lifecycle
database and the artifact blobs, all from the stack's named volumes.

**That job does not cover the vault.** The curated plane is Markdown in the
vault directory; keep it in git and push it to a private remote on a
schedule (the backup README describes this). Also copy the backup directory
off the host. A restore needs all three: Qdrant snapshots, the lifecycle
database and the vault.

Test a restore before you need one. **The repo's `deploy/backup/restore.yml` playbook does not work today**; see the warning at the top of the page below:
[backup and restore](../Musubi/09-operations/backup-restore.md) and the
[manual recovery runbook](../../deploy/runbooks/manual-recovery.md).

## Alerts

[Alerts](../Musubi/09-operations/alerts.md) lists the conditions worth paging
on, and which are dashboard-only. Wire them into whatever alerting you
already run.
