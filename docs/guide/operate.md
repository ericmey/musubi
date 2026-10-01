# Operate it

## Health

- `GET /v1/ops/health`: is Core up.
- `GET /v1/ops/status`: per-dependency status (Qdrant, the TEI services,
  Ollama), for dashboards and smoke checks.
- `GET /v1/ops/metrics`: Prometheus metrics. The stack's local Prometheus
  scrapes it; point `remote_write` at your own long-term store if you have
  one.

## Upgrades

The full procedure is the [image upgrade runbook](../../deploy/runbooks/upgrade-image.md).
Its shape:

1. **A release publishes a signed image,** and an automatic PR proposes the new
   digest for `deploy/ansible/group_vars/all.yml`. That PR never merges itself.
2. **Verify the digest** with `cosign verify`, as in
   [Install](install.md#pin-and-verify-the-image), and read the release notes
   in [CHANGELOG.md](../../CHANGELOG.md).
3. **Run the credential preflight** the PR describes. It starts the candidate
   image against your live tokens and must pass before you merge.
4. **Dry-run, then deploy.** `scripts/musubi-deploy core,lifecycle-worker`
   runs the playbook with `--check --diff` by default; only the image and
   version lines should change. Add `--apply` to deploy.
5. **Check your agents** before calling the upgrade done: a real capture and
   recall from each integration you run, not only a health check.
6. **Roll back** by reverting the pin commit and deploying again.

## Backups

[`deploy/backup/musubi-backup.sh`](../../deploy/backup/musubi-backup.sh) is a
host-local job, run by a systemd timer every six hours. It snapshots each
Qdrant collection and copies the lifecycle database and the artifact blobs
into `/var/lib/musubi/backups/<timestamp>/`, keeping 14 days.

It is written for the Ansible-deployed layout. It needs:

- a Compose project named `musubi` (set `COMPOSE_PROJECT` otherwise);
- exactly one running `lifecycle-worker` container;
- `QDRANT_API_KEY` in that container's environment, because the script calls
  Qdrant from inside it.

[Its README](../../deploy/backup/README.md) describes one deployment, where
1Password Connect injects that key at startup. Any method that sets the
variable in the container works; the script never reads secrets from host
files.

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
