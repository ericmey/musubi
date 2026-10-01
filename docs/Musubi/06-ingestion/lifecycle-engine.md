---
title: Lifecycle Engine
section: 06-ingestion
tags: [ingestion, lifecycle, scheduler, section/ingestion, status/complete, type/spec, worker]
type: spec
status: complete
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: false
implements: ["tests/lifecycle/__init__.py", "tests/lifecycle/test_lifecycle.py", "tests/lifecycle/test_runner.py"]
---
# Lifecycle Engine

The lifecycle worker is Musubi's background process. It runs maturation, synthesis, promotion, demotion, reflection and reconciliation on a schedule, on top of shared primitives: file locks, the typed `transition()` function, the lifecycle transition coordinator and LifecycleEvent recording.

## Process identity

- Compose service: `lifecycle-worker`. It uses the same image as `core` with a different command.
- Entry point: `python -m musubi.lifecycle.runner` (`src/musubi/lifecycle/runner.py`, `main()`).
- Connects to Qdrant, TEI (dense, sparse, reranker), the lifecycle LLM endpoint, the vault directory, the artifact blob directory and the shared lifecycle SQLite database.
- Exposes Prometheus `/metrics` on `LIFECYCLE_METRICS_PORT` (default `8101`). There is no HTTP API.
- Traces are emitted under `service.name=lifecycle-worker`.

It runs independently of Core. If Core goes down, the worker keeps working; if the worker goes down, Core keeps serving reads and writes. Writes that need lifecycle follow-up (artifact indexing, pending transitions) wait durably in the coordinator's outbox.

## Scheduler

The worker uses a **tick loop**, not APScheduler. APScheduler was the original design but was never added as a dependency. `LifecycleRunner` implements the subset the engine needs:

- Every tick (`min(60, LIFECYCLE_RECONCILE_INTERVAL_S)` seconds, so 5 s by default) it evaluates every job against the current **UTC** time.
- **Cron** jobs fire when the tick falls in the matching minute (`minute`, `hour`, `day`, `month`, `day_of_week`), at most once per minute. An unknown cron field raises instead of being silently ignored.
- **Interval** jobs fire once at boot and then whenever the interval has elapsed since their last dispatch.
- A job runs in a worker thread (`asyncio.to_thread`) and is not awaited by the loop, so a long sweep never blocks the next tick.
- SIGTERM and SIGINT stop the loop after the current tick.

There is **no persistent job store** and **no misfire catch-up**. If the worker is down during a job's minute, that occurrence is skipped; the next scheduled occurrence runs normally. `Job.grace_time_s` and `Job.coalesce` exist in `src/musubi/lifecycle/scheduler.py` and are exercised against the test harness (`TestingScheduler`), but the production runner does not consult them.

## Job registry

`src/musubi/lifecycle/scheduler.py` (`build_default_jobs`) declares the names and triggers. Each sweep module supplies a real `build_*_jobs` builder, and `build_lifecycle_jobs` in `runner.py` merges them. All times are UTC.

| Job | Trigger | Builder |
|---|---|---|
| `maturation_episodic` | hourly at :13 | `lifecycle/maturation.py` |
| `provisional_ttl` | hourly at :17 | `lifecycle/maturation.py` |
| `synthesis` | daily 03:00 | `lifecycle/synthesis.py` |
| `concept_maturation` | daily 03:30 | `lifecycle/maturation.py` |
| `promotion` | daily 04:00 | `lifecycle/promotion.py` |
| `demotion_concept` | daily 05:00 | `lifecycle/demotion.py` |
| `demotion_episodic` | Sundays 03:45 | `lifecycle/demotion.py` |
| `demotion_artifact` | Sundays 04:15 (no-op unless `MUSUBI_ARTIFACT_ARCHIVAL_ENABLED=true`) | `lifecycle/demotion.py` |
| `reflection_digest` | daily 06:00 | `lifecycle/reflection.py` |
| `vault_reconcile` | every 6 h (and once at boot) | `vault/reconciler.py` |
| `lifecycle_reconcile` | every `LIFECYCLE_RECONCILE_INTERVAL_S` (default 5 s, and once at boot) | `runner.py` |

The schedule is fixed in code. There are no `LIFECYCLE_SCHEDULE_*` settings; changing a cadence is a code change.

`lifecycle_reconcile` drives the transition coordinator: it applies pending lifecycle transitions and custom intents (immutable-vector publishes, artifact indexing) and cleans up terminal outbox rows older than `LIFECYCLE_CLEANUP_RETENTION_S` (30 days by default). The worker runs one reconcile pass synchronously at boot. After `LIFECYCLE_READINESS_MAX_RECONCILE_FAILURES` consecutive failures (default 3), the `musubi_lifecycle_coordinator_ready` gauge drops to 0.

## Locking

Every sweep job takes a non-blocking `fcntl.flock` before it runs:

```python
lock_path = lock_dir / f"{name}.lock"
with file_lock(lock_path) as acquired:
    if not acquired:
        log.info("lifecycle-job=%s lock-held; skipping run", name)
        return
    asyncio.run(sweep())
```

`lock_dir` is `<parent of LIFECYCLE_SQLITE_PATH>/locks`. With the documented default `LIFECYCLE_SQLITE_PATH=/var/lib/musubi/lifecycle/work.sqlite`, that is `/var/lib/musubi/lifecycle/locks/`, holding one lock file per job (`maturation_episodic.lock`, `synthesis.lock`, `promotion.lock`, `reflection.lock`, `vault_reconcile.lock`, and so on).

Why flock: the kernel releases it when the process dies, so a crash-restart cannot leave a stale lock behind. `NamespaceLock` (one lock file per job and namespace hash) exists in `scheduler.py` for namespace-partitioned work, but the shipped jobs use one lock per job.

## State

All durable worker state lives in the shared lifecycle SQLite database at `LIFECYCLE_SQLITE_PATH` (WAL mode, `LIFECYCLE_SQLITE_BUSY_TIMEOUT_MS`, default 5000). Core opens the same database:

- lifecycle transition coordinator outbox (pending transitions and intents);
- LifecycleEvent rows;
- maturation and synthesis cursors.

Next to it:

- `locks/`: job lock files;
- `vault-writelog.db`: the vault write-log shared with the vault watcher (echo filter);
- the idempotency receipt ledger, unless `IDEMPOTENCY_RECEIPT_SQLITE_PATH` overrides it.

Back up the whole directory. See [[09-operations/backup-restore]].

## LifecycleEvent recording

Every state transition goes through `transition()` in `src/musubi/lifecycle/transitions.py` (see [[04-data-model/lifecycle#transition-function]]). `LifecycleEventSink` (`src/musubi/lifecycle/events.py`) commits each event **synchronously** to SQLite and returns `Ok` only after the commit. There is no in-memory buffer to lose on a crash. A refused write is a typed error and increments `musubi_lifecycle_event_write_failures_total`.

A Qdrant mirror collection (`musubi_lifecycle_events`) is declared in the store layer but not populated. (Not implemented.)

## Failure handling

The runner wraps every dispatch:

- A crashed job increments `musubi_lifecycle_job_errors_total{job}` and logs the traceback.
- It emits an ops-alert Thought (channel `ops-alerts`, `to_presence="all"`, from `lifecycle-worker`) whose body holds the job name, the exception **class** (never the message), a UTC timestamp and the trace id. Emission is bounded to 5 s. A failed emission increments `musubi_lifecycle_job_alert_errors_total{job}` and never crashes the runner.
- The loop keeps going. The next run resumes from the job's persisted cursor.

### Crash recovery

If the worker dies mid-job:

1. The kernel releases the job's flock.
2. On restart, the boot reconcile pass drives any durable pending intents, and interval jobs fire immediately.
3. Cron jobs wait for their next scheduled minute; the missed occurrence is not replayed. Each sweep then resumes from its cursor or re-selects by state, which is why every sweep must be idempotent.

### Lifecycle LLM unavailable

- **Maturation** still transitions rows to `matured` and keeps captured values. See [[06-ingestion/maturation#Failure modes]].
- **Synthesis** skips the clusters whose LLM call failed and retries them on the next run.
- **Promotion** skips concepts whose render failed; they stay `matured`.
- **Reflection** still writes the day's file, with the patterns section replaced by a skip notice.

There is no CLI status command for the worker. Use the metrics and logs below.

## Concurrency within a job

Jobs run their work sequentially inside the sweep. Maturation batches 10 items per LLM call but issues the calls one after another. There are no `LIFECYCLE_CONCURRENCY_*` settings.

## Observability

Metrics on the worker's `/metrics`:

- `musubi_lifecycle_job_duration_seconds{job}`: histogram, observed on every dispatch;
- `musubi_lifecycle_job_errors_total{job}`: crashed dispatches;
- `musubi_lifecycle_job_alert_errors_total{job}`: failed ops-alert emissions;
- `musubi_lifecycle_coordinator_ready`: 1 when the shared store is open and reconcile is healthy;
- `musubi_lifecycle_enrichment_batch_failures_total{kind}`: maturation;
- `musubi_lifecycle_synthesis_decode_skips_total`, `musubi_lifecycle_synthesis_family_failures_total`: synthesis;
- `musubi_lifecycle_event_write_failures_total`: event sink.

Alerting on these metrics is an operator concern; no alert rules for them ship in this repository. See [[09-operations/alerts]].

## Testing

Sweep functions take their clients as arguments, so each is testable with fakes. The runner's trigger evaluation is pure (`_cron_matches`, `_interval_due`), and `LifecycleRunner` can be driven with a short `tick_seconds`. `TestingScheduler` in `scheduler.py` is the harness for grace/coalesce semantics and file-lock behaviour.

## Test Contract

**Module under test:** `src/musubi/lifecycle/runner.py`, `src/musubi/lifecycle/scheduler.py`, `src/musubi/lifecycle/transitions.py`, `src/musubi/lifecycle/events.py`

Scheduler (`tests/lifecycle/test_lifecycle.py`, `tests/lifecycle/test_runner.py`):

1. `test_jobs_registered_with_documented_triggers`
2. `test_missed_job_within_grace_runs` (test harness only)
3. `test_missed_job_outside_grace_skipped` (test harness only)
4. `test_coalesce_multiple_misfires_run_once` (test harness only)
5. `test_cron_matches_minute_only_fires_each_hour_at_that_minute`
6. `test_cron_matches_day_of_week`
7. `test_cron_matches_unknown_field_raises`
8. `test_interval_due_fires_on_first_tick`
9. `test_runner_dispatches_matching_job_once_per_minute`
10. `test_runner_isolates_job_exception`

Locking:

11. `test_file_lock_acquires_and_releases`
12. `test_second_lock_attempt_fails_fast`
13. `test_lock_released_on_process_death`
14. `test_namespace_scoped_lock_allows_parallel_namespaces`

Failure isolation (`tests/lifecycle/test_life006_alerts.py`, `tests/lifecycle/test_runner_metrics.py`):

15. `test_job_failure_does_not_stop_scheduler`
16. `test_job_failure_emits_exactly_one_durable_alert`
17. `test_alert_emission_timeout_is_bounded_and_does_not_crash_runner`
18. `test_runner_dispatch_observes_job_duration_and_errors_on_crash`

State and events:

19. `test_cursor_advances_on_successful_batch`
20. `test_cursor_persists_across_worker_restart`
21. `test_lifecycle_events_batched_and_flushed`
22. `test_events_survive_worker_restart`

Integration (not implemented):

23. `full day simulation: seed corpus, advance clock 24h, assert each scheduled job ran once`
24. `crash recovery: kill worker mid-synthesis, restart, synthesis completes from cursor`
25. `LLM-outage scenario: synthesis skips cleanly, maturation skips enrichment, alerts emit`

## Pitfalls

- **Scheduler skew.** If maturation and synthesis overlap, the per-job file locks prevent a double run, but contention slows both. Keep the cron times staggered.
- **Promotion prompt drift.** LLM output quality varies; keep the promotion prompt under test with golden examples.
- **Vault write race.** A crash between the vault write and the Qdrant write can leave the two out of sync. Writes are idempotent by `object_id`, and the `vault_reconcile` job repairs drift.
- **Event write-amplification.** Every transition is one row in the lifecycle sqlite. Nothing purges lifecycle events today, so watch that the table stays bounded.
- **Promotion-gate tuning.** In the first week the gate is either too loose (floods the vault) or too strict (nothing promotes). Monitor and tune.
