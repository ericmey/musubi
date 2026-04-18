---
title: Lifecycle Engine
section: 06-ingestion
tags: [ingestion, lifecycle, scheduler, section/ingestion, status/complete, type/spec, worker]
type: spec
status: complete
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: false
---
# Lifecycle Engine

The Lifecycle Worker process — the background brain of Musubi. Runs maturation, synthesis, promotion, demotion, reflection, reconciliation on schedule, on top of shared primitives (locks, transitions, LifecycleEvent emission).

## Process identity

- Container: `musubi-lifecycle` (see [[03-system-design/components]]).
- Python entry point: `musubi.lifecycle.worker:main`.
- Connects to: Qdrant, TEI (for re-embedding on reconcile), Ollama (for enrichment), sqlite state dir.

Runs independently of Core. If Core goes down, the Worker keeps batching; if Worker goes down, Core keeps serving queries.

## Scheduler

**APScheduler** (Python), using the `BlockingScheduler` variant — single process, multiple job types, persistent job store.

Job store: sqlite at `/srv/musubi/lifecycle-state/scheduler.db`. Survives restarts; missed jobs (while Worker was down) are either replayed (if idempotent and within grace) or skipped per job config.

## Job registry

```python
# musubi/lifecycle/jobs.py

JOBS = [
    Job(
        name="maturation_episodic",
        trigger=CronTrigger(minute=13),            # hourly
        func=maturation.run_episodic,
        grace_time_s=900,                          # if we missed by < 15m, run it
        coalesce=True,                             # if we missed several, run once
    ),
    Job(
        name="provisional_ttl",
        trigger=CronTrigger(minute=17),            # hourly, offset
        func=maturation.run_provisional_ttl,
    ),
    Job(
        name="synthesis",
        trigger=CronTrigger(hour=3, minute=0),     # daily 03:00
        func=synthesis.run_all_namespaces,
        grace_time_s=3600,
    ),
    Job(
        name="concept_maturation",
        trigger=CronTrigger(hour=3, minute=30),
        func=concept_maturation.run,
    ),
    Job(
        name="promotion",
        trigger=CronTrigger(hour=4, minute=0),
        func=promotion.run_all_namespaces,
    ),
    Job(
        name="demotion_concept",
        trigger=CronTrigger(hour=5, minute=0),
        func=demotion.run_concept,
    ),
    Job(
        name="demotion_episodic",
        trigger=CronTrigger(day_of_week="sun", hour=3, minute=45),
        func=demotion.run_episodic,
    ),
    Job(
        name="reflection_digest",
        trigger=CronTrigger(hour=6, minute=0),
        func=reflection.run,
    ),
    Job(
        name="vault_reconcile",
        trigger=IntervalTrigger(hours=6),
        func=reconcile.run,
    ),
]
```

Schedule tunables live in `config.py` (`LIFECYCLE_SCHEDULE_*`) and override the defaults.

## Locking

Every job acquires a file-lock before executing:

```python
# musubi/lifecycle/locks.py

with file_lock(f"/srv/musubi/locks/{job.name}.lock", timeout=0) as acquired:
    if not acquired:
        log.info(f"{job.name} already running; skipping")
        return
    try:
        job.func()
    finally:
        pass  # lock released on context exit
```

Rationale: APScheduler has its own concurrency control (`max_instances`), but a crash-restart while a job is running can bypass it. A file lock (via `fcntl.flock`) is inherited + reset on process death, making it robust.

For namespace-scoped jobs (synthesis, promotion), the lock is `/srv/musubi/locks/{job.name}-{ns_hash}.lock` — allows parallel namespaces.

## State: cursors + attempt tracking

Each job reads/writes a small state blob:

```
/srv/musubi/lifecycle-state/
├── scheduler.db          (APScheduler)
├── synthesis-cursor.db   (last-run per namespace)
├── maturation-cursor.db  (last-seen object_id + epoch)
├── reflection-cursor.db
├── reconcile-cursor.db
└── events.db             (LifecycleEvent local store)
```

These are small sqlite files; easy to back up / inspect.

## LifecycleEvent emission

Every state transition goes through `transition()` in `musubi/lifecycle/transitions.py` (see [[04-data-model/lifecycle#transition-function]]). Events are batched:

- Up to 100 events or 5 seconds, whichever comes first.
- Flushed to `events.db` (sqlite).
- Asynchronously mirrored to `musubi_lifecycle_events` Qdrant collection (optional — for semantic search over audit, used by reflection).

On crash: unflushed events are lost. Mitigation: we flush after every `transition()` call inside lifecycle jobs (more pessimistic than the generic API write path, because lifecycle does more state changes).

## Failure handling

Each job is wrapped in `try/except`:

```python
try:
    job.func()
except Exception as e:
    log.exception(f"job {job.name} failed")
    metrics.lifecycle_job_failure.labels(job=job.name).inc()
    emit_thought(
        to_presence="all",
        channel="ops-alerts",
        content=f"Lifecycle job {job.name} failed: {e}",
        importance=8,
    )
```

A failed job does not tear down the scheduler. The next run picks up where the cursor left off (idempotent design).

### Crash recovery

If the Worker container crashes mid-job:

1. File lock is released (flock is process-scoped).
2. APScheduler on restart: checks `misfire_grace_time` — if within grace, runs the job; else skips.
3. Job runs with its persisted cursor — resumes from last successful batch.

### Cascade failure

If Ollama is down:

- Maturation: still transitions to `matured`, skips enrichment. Re-enrichment sweep handles it later.
- Synthesis: skips entire run (needs LLM to generate). Cursor does not advance.
- Promotion: skips entire run (needs LLM to render). Concepts stay `matured`.
- Reflection: skips.

All emit `ops-alerts` Thoughts. A status check (`musubi-cli ops status`) shows which jobs were skipped recently.

## Concurrency model within a job

Each job is internally asynchronous where it helps:

- Maturation batch: async LLM calls parallelized (limit 4 in flight to avoid OOM on Ollama).
- Synthesis: parallel cluster-gen (limit 4).
- Promotion: sequential per concept (1 at a time; safer given vault writes).
- Reconciler: parallel file reads (limit 32).

Configured via `LIFECYCLE_CONCURRENCY_*` keys.

## Observability

Metrics per job:

- `lifecycle.job.duration_seconds{job}` histogram
- `lifecycle.job.failures{job,reason}` counter
- `lifecycle.job.items_processed{job}` counter
- `lifecycle.job.skipped{job,reason}` counter
- `lifecycle.job.last_success_epoch{job}` gauge

Grafana dashboard: per-job run history, duration trends, failure rate.

Alerts:

- No successful run of `maturation_episodic` in last 3 hours.
- No successful run of `synthesis` in last 48 hours.
- Any job's failure rate > 30% over 24h.

See [[09-operations/alerts]].

## Testing

Each job function is pure over its injected clients — testable in isolation:

```python
def test_maturation_run_pure_function():
    fake_client = FakeQdrant([...])
    fake_ollama = FakeOllama(...)
    result = maturation.run_episodic(fake_client, fake_ollama, now=FIXED_TIME)
    assert result.processed == 10
```

Scheduler tests use `APScheduler`'s in-memory job store and `FakeTimer`:

```python
def test_scheduler_misfires_handled():
    scheduler = build_scheduler(jobs=[...], time_travel=True)
    scheduler.advance(hours=4)
    assert scheduler.runs_count("maturation_episodic") == 4
```

## Test contract

**Module under test:** `musubi/lifecycle/worker.py`, `musubi/lifecycle/locks.py`, `musubi/lifecycle/transitions.py`

Scheduler:

1. `test_jobs_registered_with_documented_triggers`
2. `test_missed_job_within_grace_runs`
3. `test_missed_job_outside_grace_skipped`
4. `test_coalesce_multiple_misfires_run_once`

Locking:

5. `test_file_lock_acquires_and_releases`
6. `test_second_lock_attempt_fails_fast`
7. `test_lock_released_on_process_death`
8. `test_namespace_scoped_lock_allows_parallel_namespaces`

Failure isolation:

9. `test_job_failure_does_not_stop_scheduler`
10. `test_job_failure_emits_thought`
11. `test_job_failure_metric_incremented`

State:

12. `test_cursor_advances_on_successful_batch`
13. `test_cursor_persists_across_worker_restart`
14. `test_scheduler_db_persists_job_history`

Events:

15. `test_lifecycle_events_batched_and_flushed`
16. `test_events_survive_worker_restart` (sqlite is committed)

Integration:

17. `integration: full day simulation — seed corpus, advance clock 24h, assert each scheduled job ran once`
18. `integration: crash recovery — kill worker mid-synthesis, restart, synthesis completes from cursor`
19. `integration: ollama-outage scenario — synthesis skips cleanly, maturation skips enrichment, alerts emit`
