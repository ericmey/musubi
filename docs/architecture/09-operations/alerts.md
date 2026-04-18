---
title: Alerts
section: 09-operations
tags: [alerts, on-call, operations, section/operations, status/complete, type/runbook]
type: runbook
status: complete
updated: 2026-04-17
up: "[[09-operations/index]]"
reviewed: false
---
# Alerts

What fires a page, what fires an email, what's silent. Targeted for a single-operator household scope — noise hurts us more than it helps.

## Routing

Two channels:

1. **Push** (ntfy or Pushover) — urgent, wake-the-operator.
2. **Email** — next-day, read-on-coffee.
3. (No chat channel yet; could add Slack/Discord post-v1.)

Everything flows through Alertmanager on the host.

## Alert catalog

### Push (urgent)

Fire these only when something is actually broken and worse if left until morning.

| Alert | Condition | Action |
|---|---|---|
| `core_5xx_high` | Core 5xx rate > 1% for 5m | Check Core logs; restart if needed |
| `qdrant_down` | Qdrant `/healthz` failing for 2m | See runbook [[09-operations/runbooks#qdrant-down]] |
| `vault_fs_full` | `/var/lib/musubi` < 10% free | Clear logs or expand disk |
| `backup_failure_24h` | No Qdrant snapshot in 24h | Manual snapshot + investigate cron |
| `gpu_oom` | Any container OOMKilled | Restart; investigate model sizes |
| `loop_detected` | Vault echo filter catching > 100/min | Pause sync; investigate |
| `token_signing_key_missing` | JWT signing key file missing | Restore from 1Password; all auth fails |

### Email (informational, next-morning)

| Alert | Condition |
|---|---|
| `lifecycle_job_failing` | Same job errors 3 times in a row |
| `synthesis_skipped_many_clusters` | > 20% of clusters skipped due to LLM errors |
| `promotion_stale` | Concepts eligible but no promotions for 48h |
| `contradiction_unresolved_7d` | A contradiction has sat unresolved for 7 days |
| `high_provisional_backlog` | > 500 provisional memories older than 7 days |
| `eval_regression` | Nightly eval dropped NDCG@10 by > 5% |
| `disk_growth_anomaly` | `/var/lib/musubi` grew > 2x expected rate this week |
| `snapshot_retention_purge_failed` | Retention cron couldn't delete old snapshots |

### Silent (logged, not alerted)

Lower-signal conditions — surfaced in dashboards but not alerted:

- Single retrieval > 5s (tracked; only alert if sustained).
- Dedup rate > 30% (usually means similar captures are coming in fast; not inherently bad).
- Thought history search miss.
- Any WARN log line not in the catalog above.

## Thresholds — how we chose them

**Core 5xx > 1% for 5m:** at typical ~100 req/min, 1% is 1 error/min. Sustained 5 min = five errors. Enough signal to be real, low enough false positive rate.

**Qdrant down 2m:** long enough to survive a restart (WAL replay ~30s), short enough to act before episodic captures time out.

**GPU OOM:** we never expect this; if it fires, something got misconfigured or a memory leak appeared.

**Backup failure 24h:** one missed snapshot (6h) is tolerable. Missing all snapshots for 24h means the cron is broken — urgent, since data at risk.

**Synthesis skipped > 20%:** means the LLM is flaky or a bad prompt is poisoning many clusters. Worth investigating before the next run.

**Promotion stale 48h:** our concepts normally mature at least one candidate every couple days. If 48h pass with no promotions, either reinforcement tracking is broken or the LLM is failing silently.

## Suppression

During planned maintenance, silence alerts:

```bash
amtool silence add \
  alertname=~"core_5xx_high|qdrant_down" \
  --duration=1h \
  --comment="planned compose update"
```

Silences auto-expire. Never silence without a comment.

## Runbook linkage

Every push alert has a linked runbook in [[09-operations/runbooks]]:

```yaml
# alertmanager template snippet
annotations:
  summary: "Qdrant is not responding"
  runbook: "https://docs.musubi.internal/09-operations/runbooks/#qdrant-down"
```

If an alert doesn't have a runbook, it doesn't fire. Period.

## Testing alerts

**Quarterly chaos drill:**

- Kill Qdrant → expect `qdrant_down` in ~2m.
- `dd` fill `/var/lib/musubi` to 91% → expect `vault_fs_full`.
- Trigger 5xx in Core via a test endpoint → expect `core_5xx_high`.

Log expected behavior + actual behavior. If an alert doesn't fire when it should, fix the rule and re-drill.

## What we don't page on (deliberately)

- **Eval regressions** (email only, next-morning).
- **Contradictions** (informational; human needs to decide — no rush).
- **Large tag/topic tail** (not urgent; cleanup on a schedule).
- **Any single slow request** (flap-prone; only aggregates).

## On-call model

One operator (Eric). When unavailable:

- Alerts buffer in ntfy.
- After 30 min un-acked, escalate to email.
- No automatic failover of on-call (household scope; not a service with external customers).

If the system is unreachable for > 30 min, captures queue in adapter clients ([[07-interfaces/openclaw-adapter#offline-behavior]], [[07-interfaces/livekit-adapter#error-handling]]) and drain on recovery.

## Alertmanager config (excerpt)

```yaml
route:
  receiver: default
  group_by: [alertname]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 1h
  routes:
    - matchers: [severity="push"]
      receiver: ntfy
      repeat_interval: 30m
    - matchers: [severity="email"]
      receiver: email
      repeat_interval: 24h

receivers:
  - name: ntfy
    webhook_configs:
      - url: "https://ntfy.sh/musubi-alerts"
  - name: email
    email_configs:
      - to: eric@example.com
        from: alertmanager@musubi.internal
        smarthost: smtp.example.com:587
```

## Test contract

1. `test_every_push_alert_has_linked_runbook`
2. `test_every_alert_has_for_clause_to_dedupe_flaps`
3. `test_alertmanager_config_loads_without_error`
4. `test_chaos_drill_qdrant_down_fires_within_3m` (integration)
5. `test_backup_failure_alert_fires_after_24h_gap`
6. `test_silent_alerts_still_show_on_dashboard`
