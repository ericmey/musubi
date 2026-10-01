---
title: Runbooks
section: 09-operations
tags: [incident-response, operations, runbooks, section/operations, status/draft, type/runbook]
type: runbook
status: draft
updated: 2026-10-01
up: "[[09-operations/index]]"
reviewed: false
---
# Runbooks

Step-by-step procedures for the recommended alerts ([[09-operations/alerts]]) and common
operator actions. Numbered steps, copy-pasteable commands.

All commands run on the Musubi host, from the repo checkout that holds your
`docker-compose.yml` and `.env`. If you started the stack with the GPU overlay, add the
same `-f docker-compose.yml -f deploy/docker/compose.local-gpu.yml` to every
`docker compose` command. Qdrant, TEI and Ollama publish no host ports; reach them through
`docker compose exec`.

**Canary** (used below): `deploy/smoke/verify.sh` checks health, status, a capture and
retrieve round trip, a thought round trip and the metrics endpoint. It does not clean up:
every run leaves a captured memory and a thought behind. So give it a dedicated smoke
identity that no agent uses, with a token scoped only to it (see `docs/guide/connect.md`),
and its rows stay out of real agent memory:

```bash
MUSUBI_BASE_URL=http://127.0.0.1:8100 MUSUBI_TOKEN=<smoke-canary-token> \
MUSUBI_NAMESPACE=smoke/canary/episodic MUSUBI_THOUGHT_NAMESPACE=smoke/canary/thought \
MUSUBI_PRESENCE=smoke/canary bash deploy/smoke/verify.sh
```

## First deploy

Install with `docs/guide/install.md`: prepare the host, fill in `.env`, then
`docker compose up -d --wait` (plus the GPU overlay if you use it). Success criteria:
`curl -fsS http://127.0.0.1:8100/v1/ops/health` returns `{"status":"ok",…}`,
`/v1/ops/status` reports every component healthy, and the canary passes.

`deploy/runbooks/first-deploy.md` is an older operator runbook for a different,
host-provisioned layout with its own gateway. It is not the public install path; keep it as
context only.

## Core down

**Alert:** `core_down`

1. `docker compose ps` — is `core` running and healthy?
2. If it is not running: `docker compose up -d --wait core`.
3. If it keeps restarting: `docker compose logs --tail 200 core`. A startup that fails on
   Qdrant or the dense TEI endpoint means Core's dependency probe failed; go to
   [[09-operations/runbooks#qdrant-down]], or check `TEI_DENSE_URL`.
4. Verify: `curl -fsS http://127.0.0.1:8100/v1/ops/health`, then run the canary.

## Qdrant down

**Alert:** `qdrant_down` (`/v1/ops/status` reports `qdrant` unhealthy for 2m)

1. `docker compose ps qdrant` — is the container running?
2. If not: `docker compose up -d qdrant`, wait 60s, then step 4.
3. If it is running but unhealthy: `docker compose logs --tail 200 qdrant`. Common causes:
   - Disk full → [[09-operations/runbooks#vault-fs-full]].
   - Corrupt collection → restore; see [[09-operations/backup-restore]].
   - A bad `.env` change (e.g. `QDRANT_API_KEY` changed for Qdrant but not for Core, or the
     reverse; both read the same variable) → revert it and `docker compose up -d`.
4. Confirm Qdrant answers from inside the network:
   `docker compose exec core curl -fsS http://qdrant:6333/healthz`.
5. Verify Core sees it: `curl -fsS http://127.0.0.1:8100/v1/ops/status` shows
   `qdrant` healthy. Then expire any silence you set.

## Core 5xx high

**Alert:** `core_5xx_high` (5xx ratio > 1% for 5m)

1. Recent errors: `docker compose logs --since 10m core | grep '"level": "error"'`.
2. Which endpoint? Check `musubi_5xx_total` by `endpoint` on `/v1/ops/metrics`.
3. Which dependency? `curl -fsS http://127.0.0.1:8100/v1/ops/status`:
   - `qdrant` unhealthy → [[09-operations/runbooks#qdrant-down]].
   - a `tei-*` component unhealthy → restart it if you run the overlay
     (`docker compose … restart tei-dense`), or check the remote endpoint.
   - `ollama` unhealthy → not on the request path; see
     [[09-operations/runbooks#ollama-stalled]].
4. If the error is new or unknown: save the log lines (they carry `request_id`), open an
   issue, then `docker compose restart core`.
5. Verify with the canary. If 5xx persists after the restart, take Core out of your
   reverse proxy and investigate without load.

## Lifecycle worker not ready

**Alert:** `lifecycle_worker_not_ready`

1. `docker compose ps lifecycle-worker` — running and healthy?
2. `docker compose logs --tail 200 lifecycle-worker` — look for errors opening
   `/var/lib/musubi/lifecycle/work.sqlite` or reaching Qdrant.
3. If the `lifecycle` volume's filesystem is full → [[09-operations/runbooks#vault-fs-full]].
4. `docker compose restart lifecycle-worker`, then verify its health check passes
   (`docker compose ps`).

## Vault fs full

**Alert:** `vault_fs_full` (< 10% free on the filesystem holding Docker's volumes)

1. Find Docker's data directory: `docker info --format '{{.DockerRootDir}}'`, then
   `df -h` on it.
2. See which volume is large: `docker system df -v`.
3. Typical suspects:
   - `musubi_artifact-blobs` grew → check recent uploads.
   - `musubi_qdrant-snapshots` → delete old snapshots with Qdrant's
     `DELETE /collections/<name>/snapshots/<snapshot>` (via `docker compose exec core curl …`).
   - Local backup sets in the checkout → move them off the host.
   - Container logs → tighten your Docker logging driver's rotation settings.
4. If every volume is within its expected size, the disk is just full: grow it.
5. Verify writes work again with the canary.

## GPU OOM

**Alert:** `gpu_oom` (GPU overlay only)

1. `nvidia-smi` on the host — what is using VRAM now?
2. `docker compose ps -a` — which of `tei-dense`, `tei-sparse`, `tei-reranker`, `ollama`
   exited or keeps restarting?
3. `docker compose logs --tail 100 <service>` — look for CUDA out-of-memory.
4. Restart it: `docker compose up -d <service>`.
5. Root cause:
   - A larger model or a new image tag? Revert the `.env` change.
   - Batch size grew? Lower `--max-batch-tokens` in the overlay.
6. If it recurs, review the budget in [[08-deployment/gpu-inference-topology]]. Verify with
   the canary once all four services are healthy.

## Loop detected

**Alert:** none. Musubi emits no metric for vault write echoes (planned, not implemented).

This can only happen if you run the optional standalone vault watcher
(`python -m musubi.vault.watcher`), which the public stack does not start: the watcher
re-reads a file the lifecycle worker just wrote as if a human had edited it.

1. Stop the watcher process.
2. Count unconsumed write-log entries:

   ```bash
   docker compose exec lifecycle-worker python -c "
   import sqlite3
   db = sqlite3.connect('/var/lib/musubi/lifecycle/vault-writelog.db')
   print(db.execute('select count(*) from writes where consumed_at is null').fetchone()[0])"
   ```

3. If the count keeps growing while the watcher runs, the watcher is not marking entries
   consumed: file a bug with the count and the affected paths.
4. Restart the watcher and verify the vault stops churning (no repeated edits to the same
   files in `git status` or your editor).

## Backup failure 24h

**Alert:** `backup_failure_24h`

1. Check your backup tool's log for the last run.
2. Common causes:
   - The stack failed to stop or restart around the backup → `docker compose ps`.
   - The backup target is full or unmounted.
3. Run the cold backup by hand: [[09-operations/backup-restore]].
4. Verify the new set: all six archives present and `sha256sum -c SHA256SUMS` passes.
5. Confirm the next scheduled run succeeds.

## Ollama stalled

Not alerting; lifecycle jobs retry on their next schedule.

1. `docker compose exec ollama ollama list` (GPU overlay) — does it respond, and is the
   `LLM_MODEL` model present?
2. If not: `docker compose restart ollama`, and pull the model again if it is missing.
3. If it responds but generation hangs: `docker compose logs --tail 100 ollama`, then
   restart it.
4. Verify: `/v1/ops/status` shows `ollama` healthy. The skipped lifecycle work runs on the
   next tick.

## Promotion failed (LLM returns garbage)

Not a page. A failing lifecycle job tick posts an `ops-alerts` Thought.

1. Inspect the concept: `GET /v1/concepts/<id>?namespace=<namespace>` with an operator
   token.
2. See its lifecycle history in the `lifecycle_events` table of the lifecycle SQLite
   ledger (`work.sqlite` in the `lifecycle` volume). The `GET /v1/lifecycle/events` routes
   are stubs that return an empty list today ([[10-security/audit]]).
3. Decide:
   - Content is nonsense → reject it (below).
   - It is fine but the gate was too strict → tune the promotion settings
     ([[06-ingestion/promotion]]).
4. It is retried on the next daily promotion run.

## Restore from snapshot

See [[09-operations/backup-restore]]: it is the authoritative procedure (cold restore of
the whole set, or one Qdrant collection from a snapshot). Do not use
`deploy/backup/restore.yml`; it does not work.

## Planned compose update

1. Silence alerts in your alerting tool, with a comment and an expiry.
2. Take a cold backup ([[09-operations/backup-restore]]).
3. Review the new pin, then `docker compose pull` and `docker compose up -d --wait`
   (details: `docs/guide/operate.md`, "Upgrades").
4. `docker compose ps` — every service up and healthy?
5. Run the canary. If it fails, roll back to the previous pin and
   `docker compose up -d --wait`.
6. Expire the silence and watch the dashboards for 10 minutes.

## Full host rebuild

Rare. See the full-disaster steps in [[09-operations/backup-restore]]. Briefly: prepare the
host, clone the repo at the same release, restore `.env`, restore the latest backup set,
start, run the canary, and re-mint tokens if the signing key was lost.

## Add a new presence

1. Choose the presence, e.g. `alex/mobile-chat`.
2. Mint a token with `sub` and `presence` both `alex/mobile-chat` and the scopes it needs,
   e.g. `alex/mobile-chat/*:rw alex/shared/curated:r` (see `docs/guide/connect.md` and
   [[10-security/auth]]).
3. Hand the token to the agent through its secret store.

## Rotate the signing key

Core verifies HS256 tokens with one key, `JWT_SIGNING_KEY`; there is no dual-key overlap.

1. Set a new `JWT_SIGNING_KEY` in `.env`.
2. `docker compose up -d --wait` (Core and the worker pick up the new value).
3. Every existing token is now rejected: re-mint and redeploy each agent's token.
4. Verify with the canary using a freshly minted token.

## Tune retrieval

If users report "I can't find X":

1. Reproduce with `POST /v1/retrieve` and note any `warnings` in the response.
2. Check `musubi_retrieval_warnings_total` and `musubi_reranker_degradation_causes_total`
   for degraded legs.
3. If nothing is degraded, add the case to your evaluation set; see `src/musubi/evals/`.

## Manually promote a concept

To fast-track a matured concept, with an operator token in `MUSUBI_TOKEN`:

```bash
musubi promote force <concept-id> --namespace <tenant>/<presence>/concept \
  --curated-id <curated-id> --reason "operator-force"
```

The curated row must exist first (create it with `POST /v1/curated`). The CLI calls
`POST /v1/concepts/<id>/promote`; it defaults to `http://localhost:8100/v1`
(`--api-url` / `MUSUBI_API_URL` to change it). The `musubi` CLI is also on the `PATH`
inside the Core image.

## Manually reject a concept

```bash
musubi promote reject <concept-id> --namespace <tenant>/<presence>/concept \
  --reason "LLM hallucination"
```

This records the rejection and bumps `promotion_attempts`; three rejections lock the
concept out of further promotion sweeps.

## Cold-start latency investigation

If retrieval p95 spiked suddenly:

1. `curl -fsS http://127.0.0.1:8100/v1/ops/status` — any dependency unhealthy?
2. GPU overlay: `nvidia-smi` — VRAM near full? → [[09-operations/runbooks#gpu-oom]].
3. Did something change? `docker compose ps` (container ages) and your deploy history.
4. If tracing is on, find a slow `retrieve.orchestration` span in your trace backend.

## Reset a misconfigured collection

There is no reset or rebuild command for collections (planned, not implemented). Restore
from a backup set or a collection snapshot instead ([[09-operations/backup-restore]]).
Never delete `musubi_episodic`, `musubi_concept`, `musubi_thought` or `musubi_artifact`:
they hold the only copy of their data.

## Quarterly game-day drills

Cycle through one operations drill each quarter so recovery paths stay fresh:

1. Q1 — `Qdrant down`: stop Qdrant on a scratch stack, follow the runbook above, and
   verify `/v1/ops/status`.
2. Q2 — `Restore from snapshot`: restore the latest cold backup set into a scratch stack
   ([[09-operations/backup-restore]]) and run the canary. Do not use `restore.yml` or
   `drill.yml`: `restore.yml` does not work today.
3. Q3 — `Backup failure 24h`: break the backup target on purpose and verify the alert and
   the manual backup path.
4. Q4 — `First deploy`: rehearse the install from `docs/guide/install.md` on a disposable
   VM, then an upgrade and rollback from `docs/guide/operate.md`.

Success criteria: the drill owner records the runbook used, the command log, the observed
recovery time, and any follow-up issue before closing the drill.

## Test contract

**Module under test:** the runbooks (readiness, not code),
`tests/ops/test_first_deploy_smoke.py`.

1. `test_every_alert_has_a_runbook_section`
2. `test_runbooks_reference_real_files_and_commands`
3. `test_each_runbook_lists_success_criteria`
4. `test_quarterly_game_day_drills_cycle_through_runbooks`
