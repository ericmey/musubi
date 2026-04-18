---
title: "Phase 6: Lifecycle"
section: 11-migration
tags: [lifecycle, migration, phase-6, scheduler, section/migration, status/stub, type/migration-phase]
type: migration-phase
status: stub
updated: 2026-04-17
up: "[[11-migration/index]]"
prev: "[[11-migration/phase-5-vault]]"
next: "[[11-migration/phase-7-adapters]]"
reviewed: false
---
# Phase 6: Lifecycle

Stand up the lifecycle engine (APScheduler + lifecycle events) with the core jobs: maturation, synthesis, promotion, demotion, reflection.

## Goal

Every state transition happens through the lifecycle engine and emits an event. The vault gets auto-written curated docs via promotion. Humans review and edit as needed.

## Changes

### APScheduler + file-lock

See [[06-ingestion/lifecycle-engine]]. Single-host, single-scheduler via `fcntl.flock` lock file.

### Schedule

- Hourly: maturation ([[06-ingestion/maturation]]).
- Daily 02:00: concept synthesis ([[06-ingestion/concept-synthesis]]).
- Daily 03:30: promotion ([[06-ingestion/promotion]]).
- Weekly Sunday 01:00: demotion ([[06-ingestion/demotion]]).
- Daily 06:00: reflection ([[06-ingestion/reflection]]).
- Every 6h: vault reconciler.

### LifecycleEvent table

Create in `lifecycle-work.sqlite`:

```sql
CREATE TABLE lifecycle_events (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  object_id TEXT NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT,
  job_id TEXT,
  request_id TEXT,
  extra TEXT  -- JSON
);
CREATE INDEX ix_lifecycle_object ON lifecycle_events(object_id);
CREATE INDEX ix_lifecycle_ts ON lifecycle_events(ts);
```

### Concept plane population

Synthesis job creates concepts. See [[06-ingestion/concept-synthesis]]. Tweak parameters post-launch based on observed cluster sizes.

### Promotion to vault

Gate check → LLM render → pydantic validate → write-log + vault write. See [[06-ingestion/promotion]].

### Demotion

Weekly episodic demotion + daily concept stale check. See [[06-ingestion/demotion]].

### Reflection

Daily generation of `vault/reflections/YYYY-MM/YYYY-MM-DD.md`. User reads + optionally comments.

### Operator endpoints

Expose `/v1/lifecycle/events` + `/v1/lifecycle/transition` + `/v1/lifecycle/reconcile` behind operator scope.

## Done signal

- All jobs configured + lockable.
- Maturation transitions provisional → matured on a test memory.
- Synthesis produces at least one candidate concept from seed data.
- Promotion writes a curated `.md` file to the vault (`concepts/<slug>.md`).
- Promotion's write-log entry is consumed by the watcher without re-indexing.
- `/v1/lifecycle/events` returns the transition history.

## Rollback

Disable the scheduler. Already-created concepts + events remain (they're data). Future state transitions are operator-manual via API.

To undo a bad promotion: operator-delete the curated file + lifecycle-events records.

## Smoke test

```
# Manually trigger maturation:
musubi-cli lifecycle run --job maturation --now

# Check events:
curl -H "Authorization: Bearer $OP" http://localhost:8100/v1/lifecycle/events?limit=20 | jq .

# Force a concept promotion on seed data:
musubi-cli lifecycle run --job promotion --target <concept-id> --force

# Look in the vault:
ls vault/concepts/
```

## Estimate

~3 weeks. This is the richest phase; lots of prompts + pydantic schemas + LLM handling + vault write correctness + echo prevention coordination.

## Pitfalls

- **Scheduler skew.** If maturation + synthesis overlap, the locks prevent double-run, but contention slows things. Stagger times.
- **Promotion prompt drift.** LLM output quality varies; put the prompt under test with golden examples.
- **Vault write race.** Between vault write and Qdrant write, a crash could leave vault+Qdrant out of sync. Idempotency via object_id; reconciler fixes drift.
- **Events write-amplification.** Every transition = a row. Audit that number stays bounded; purge per retention.
- **Promotion-gate tuning.** First week, gate will be either too loose (floods vault) or too strict (nothing promotes). Monitor + tune.
