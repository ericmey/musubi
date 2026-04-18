---
title: Runbooks
section: 09-operations
tags: [incident-response, operations, runbooks, section/operations, status/research-needed, type/runbook]
type: runbook
status: research-needed
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: false
---
# Runbooks

Step-by-step procedures for each alert and common operator actions. Read-while-half-asleep format: numbered steps, copy-pasteable commands.

## Qdrant down

**Alert:** `qdrant_down` (Qdrant `/healthz` failing for 2m)

1. SSH to host: `ssh <musubi-host>`.
2. Check container status: `docker ps -a | grep qdrant`.
3. Is the container running? If not → step 4. If yes → step 6.
4. Start: `docker compose -f /etc/musubi/docker-compose.yml up -d qdrant`.
5. Wait 60s, then `curl http://localhost:6333/healthz`. If 200 → done. If not → step 8.
6. Check logs: `docker logs musubi-qdrant-1 --tail 200`.
7. Common causes:
   - Disk full → see [[09-operations/runbooks#vault-fs-full]].
   - Segment corruption → see step 8.
   - Config parse error → revert Ansible change and `ansible-playbook musubi.yml`.
8. If corruption: restore from snapshot. See [[09-operations/backup-restore#restore-full]].
9. Once Qdrant is healthy, verify Core came back: `curl http://localhost:8100/v1/ops/health`.
10. Clear silence: `amtool silence expire …`.

## Core 5xx high

**Alert:** `core_5xx_high` (5xx rate > 1% for 5m)

1. Check recent errors: `journalctl -t musubi.core --since="10 min ago" | grep -i error`.
2. Is it a specific endpoint? Grep for `endpoint=` in logs.
3. Which downstream is involved? Common patterns:
   - `qdrant unreachable` → see [[09-operations/runbooks#qdrant-down]].
   - `tei_dense timeout` → TEI health; `docker restart musubi-tei-dense-1`.
   - `ollama` errors → non-hot-path; see [[09-operations/runbooks#ollama-stalled]].
4. If the error is new/unknown: capture a stacktrace, open an issue, then restart Core: `docker restart musubi-core-1`.
5. If 5xx persists after restart: stop accepting traffic by removing Kong's route (or return 503 from Kong temporarily), investigate without load.

## Vault fs full

**Alert:** `vault_fs_full` (< 10% free)

1. `df -h /var/lib/musubi`.
2. `du -sh /var/lib/musubi/*/` to see which subdir is the culprit.
3. Typical suspects:
   - `artifact-blobs/` grew unexpectedly → check recent uploads; consider purging large artifacts.
   - `qdrant/snapshots/` → prune: `find /var/lib/musubi/qdrant/snapshots -mtime +7 -delete`.
   - `/var/log/musubi` → rotate: `journalctl --vacuum-time=7d`.
4. If all subdirs are within expected range, disk really is just full → expand or add drive.
5. After freeing space, confirm Core resumes writes: `musubi-cli capture test --ns test/ops`.

## GPU OOM

**Alert:** `gpu_oom`

1. `nvidia-smi` — confirm which process got killed.
2. Check Compose: `docker ps -a | grep -v Up` — the killed container shows as Exited.
3. `docker logs <container> --tail 100` — look for "out of memory" or CUDA OOM.
4. Restart the killed container: `docker start <container>`.
5. Investigate root cause:
   - Did a new model deploy with bigger VRAM footprint? Revert.
   - Did batch size grow? Reduce `--max-batch-tokens`.
   - Is Ollama queue backed up? `OLLAMA_NUM_PARALLEL=1` must be set.
6. If recurring, review [[08-deployment/gpu-inference-topology#vram-budget]] and adjust.

## Loop detected

**Alert:** `loop_detected` (vault echo filter > 100/min)

This means Core is writing to the vault AND the watcher is re-reading those writes as if human-authored.

1. Pause the Vault Watcher: `musubi-cli vault pause-watcher --duration=15m`.
2. Inspect the write-log: `sqlite3 /var/lib/musubi/lifecycle-work.sqlite "select count(*) from write_log where consumed_at is null"`.
3. If count grows unbounded → watcher isn't marking entries consumed; file a bug, reset: `sqlite3 ... "update write_log set consumed_at=unixepoch() where consumed_at is null"`.
4. Review recent promotions — did we write with wrong file path, causing an inotify miss?
5. Resume watcher: `musubi-cli vault resume-watcher`.
6. Monitor echo filter rate for 30 min.

## Backup failure 24h

**Alert:** `backup_failure_24h`

1. Check the cron: `systemctl status cron`; `crontab -l -u musubi`.
2. Check the snapshot cron log: `journalctl -t qdrant-snapshot --since="36h ago"`.
3. Common causes:
   - `/mnt/snapshots` not mounted. `mount | grep snapshots`. Re-mount.
   - Qdrant api key mismatch. Check `.env`.
   - Disk full on SATA SSD. Prune: `find /mnt/snapshots/qdrant -mtime +90 -delete`.
4. Run snapshot manually: `/opt/musubi/qdrant-snapshot.sh`.
5. Verify file created + rsync'd to `/mnt/snapshots/qdrant/<ts>/`.
6. Watch the next cron fires.

## Ollama stalled

Non-alerting, but common during synthesis.

1. `curl http://localhost:11434/api/tags` — is Ollama responding?
2. If not: `docker restart musubi-ollama-1`; wait ~30s for model reload.
3. If it responds but generation hangs: `docker logs musubi-ollama-1 --tail 100`.
4. Kill in-flight generation if hung: `docker restart`.
5. Skip the current synthesis run; it retries tomorrow.
6. If this recurs, consider a watchdog: kill generation > 60s and mark the cluster for retry.

## Promotion failed (LLM returns garbage)

Not a page; a Thought arrives in ops inbox.

1. Read the Thought. It links to the concept.
2. Inspect the concept: `curl http://localhost:8100/v1/concepts/<id>`.
3. See the LLM's render output in lifecycle events: `musubi-cli lifecycle events --object <id>`.
4. Decision:
   - Pydantic validation failed → fix the render prompt (see [[06-ingestion/promotion]]).
   - Content is nonsense → reject the concept: `musubi-cli concept reject <id>`.
   - Actually it's fine but gate was too strict → tune gate in config.
5. Re-trigger: `musubi-cli lifecycle run --job promotion --target <id>`.

## Restore from snapshot

See [[09-operations/backup-restore#restore]] — that's the authoritative runbook.

## Planned compose update

1. Announce in #ops (if you have a channel).
2. Silence alerts: `amtool silence add alertname=~".+" --duration=45m --comment="planned update"`.
3. Snapshot first: `/opt/musubi/qdrant-snapshot.sh`.
4. Pull new digests: `ansible-playbook playbooks/update.yml`.
5. Watch for any container that failed to come back: `docker ps -a`.
6. Smoke test: `pytest --contract=smoke --musubi-url=http://localhost:8100/v1`.
7. Un-silence: `amtool silence expire …`.
8. Watch dashboards for 10m.

## Full host rebuild

Rare. See [[09-operations/backup-restore#full-disaster-recovery]]. Briefly:

1. Provision new Ubuntu box.
2. Run `ansible-playbook musubi.yml`.
3. Restore vault from git, Qdrant from snapshot, blobs from rsync, sqlite from backup.
4. Smoke test.
5. Re-issue OAuth tokens if signing key was lost.

## Add a new presence

1. User decides presence name (e.g., `eric/mobile-chat`).
2. Mint an OAuth client + scope in the auth authority.
3. Add scope: `eric/mobile-chat/episodic:rw`, `eric/_shared/curated:r`, etc.
4. Register in the presence registry (future config; for v1 just document).
5. Hand token to the adapter.

## Rotate tokens

1. Generate new signing key.
2. Deploy to Core with **both old and new** keys configured (dual-verify).
3. All new tokens signed with new key; old tokens continue validating.
4. Once all clients re-auth (24-48h), remove old key.
5. Old tokens reject.

## Tune retrieval

If users report "I can't find X":

1. Run `musubi-cli eval run golden-sets/*.yaml`.
2. Compare NDCG@10, MRR, Recall@20 to baseline.
3. If metrics are flat but user complaint is real → missing golden case; capture it.
4. If metrics regressed → git blame recent config changes.

## Manually promote a concept

Sometimes we want to fast-track a concept that keeps getting reinforced but hasn't hit the gate thresholds:

```
musubi-cli concept promote --id <concept-id> --force
```

Force skips the gate. Use sparingly; still goes through the LLM render pipeline and vault write-log. Recorded as a `LifecycleEvent` with `actor: operator, reason: force_promotion`.

## Manually reject a concept

If a concept is junk:

```
musubi-cli concept reject --id <concept-id> --reason "LLM hallucination"
```

State becomes `rejected`. Reinforce events are ignored henceforth.

## Cold-start latency investigation

If retrieve p95 spiked suddenly:

1. Check GPU: `nvidia-smi`. VRAM utilization? If ~ full → see [[09-operations/runbooks#gpu-oom]].
2. Check TEI health: `curl http://localhost:8010/health` (dense), `:8011` (sparse), `:8012` (reranker).
3. Check Qdrant: `curl http://localhost:6333/healthz`.
4. Check recent deploys: `git log ansible/ --since="24h ago"`.
5. Trace a sample slow request via OTel: Grafana → Tempo → filter by duration > 1s.

## Reset a misconfigured collection

**Danger.** Only if you know the collection is derived + rebuildable.

```
musubi-cli qdrant reset --collection musubi_curated --confirm
musubi-cli index rebuild --collection musubi_curated --source vault
```

Never run this on `musubi_episodic` or `musubi_concept` — they are canonical.

## Test contract

**Module under test:** the runbooks (readiness, not code)

1. `test_every_alert_has_a_runbook_section`
2. `test_runbooks_reference_real_files_and_commands` (lint)
3. `test_each_runbook_lists_success_criteria`
4. `test_quarterly_game-day_drills_cycle_through_runbooks`
