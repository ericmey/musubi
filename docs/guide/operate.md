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
   digest on every Musubi image line in `docker-compose.yml`.
   That PR never merges itself.
2. **Verify the digest** with `cosign verify`, as in
   [Install](install.md#pin-and-verify-the-image), and read the release notes
   in [CHANGELOG.md](../../CHANGELOG.md).
3. **Upgrade:** pull the reviewed pin, then `docker compose pull` and
   `docker compose up -d --wait` (with the same `-f` files you started with).
4. **Run a canary** before calling the upgrade done: with an authorized agent
   token, capture one memory and retrieve it. Do this for each integration you
   run; a health check alone is not enough.
5. **Roll back if the canary fails:** return `docker-compose.yml` to the
   previous pin and run `docker compose up -d --wait` again.

## Backups

Musubi keeps its state in the stack's named Docker volumes:
`qdrant-storage`, `qdrant-snapshots`, `vault`, `artifact-blobs`, `lifecycle`
and `logs`. Back them up **cold**, as one set:

1. **Stop the stack:** `docker compose stop`.
2. **Archive every volume** with your usual backup tool, at the same point in
   time.
3. **Start the stack again:** `docker compose up -d --wait`.

To restore, stop the stack, restore **all** of the volumes from the same backup
set (never a mix of sets), start it, then check `/v1/ops/health` and run a
canary capture and retrieve. There is no packaged backup helper yet.

The `vault` volume holds the curated plane as Markdown. Besides the volume
backup, it is worth keeping it in git and pushing to a private remote on a
schedule, so curated knowledge has its own history. Keep backups off the host.

Test a restore, following the steps above, before you need one.

## Alerts

[Alerts](../Musubi/09-operations/alerts.md) lists the conditions worth paging
on, and which are dashboard-only. Wire them into whatever alerting you
already run.
