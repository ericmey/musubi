---
title: "ADR 0008: Qdrant + sqlite Only; No Postgres"
section: 13-decisions
tags: [adr, architecture, section/decisions, status/accepted, storage, type/adr]
type: adr
status: accepted
updated: 2026-04-17
up: "[[13-decisions/index]]"
reviewed: false
---
# ADR 0008: Qdrant + sqlite Only; No Postgres

**Status:** accepted
**Date:** 2026-03-17
**Deciders:** Eric

## Context

Musubi state breaks down to:

- Vectors + payload → **Qdrant**.
- Vault markdown files → **filesystem + git**.
- Artifact blobs → **content-addressed files on disk**.
- Lifecycle events, write-log, auth state → ???

That last bucket is the only place a traditional relational store would go. Postgres is the default reflex. Do we need it?

Candidates for a relational store:

1. `lifecycle_events` — append-mostly, small, single writer.
2. `write_log` — append-then-read, tiny rows, single writer.
3. `auth_tokens` / client registry — small, low QPS.
4. Future: user preferences, presence config, rate-limit counters.

None of these have:

- High concurrency writes needing MVCC.
- Complex joins across huge tables.
- Replication / HA requirements (v1 is single-host, ADR 0010).
- Transactions spanning multiple tables.

sqlite handles this workload trivially, with:

- Zero ops overhead (one file, no daemon).
- Easy backups (`.backup`).
- Fast enough for our scale (thousands of events/day, not thousands/sec).

## Decision

**No Postgres (or any other relational server) in v1.** sqlite fills every relational-ish role:

- `/var/lib/musubi/sqlite/lifecycle.db` — `lifecycle_events`, `write_log`, `vault_sync_state`.
- `/var/lib/musubi/sqlite/auth.db` — client registry, token issuance log.

Both are WAL-mode for concurrent reads during writes. Both are backed up nightly via `.backup` ([[09-operations/backup-restore]]).

## Alternatives

**A. Postgres.** Overkill. Introduces a daemon, a backup story, connection pooling, a version dependency. No benefit at our scale.

**B. Redis for ephemeral state.** Would be fine for rate-limit counters but we don't need low-latency cross-request state in v1. When we do, reconsider.

**C. Everything in Qdrant payload.** Some things (lifecycle events) are append-heavy and would pollute the point collections.

**D. Just files.** We could store events as JSONL files on disk. Loses indexed queries ("all events for object_id X in the last 30 days"). sqlite gives us those for free.

## Consequences

- Ansible role for `sqlite` is trivial (install package, create files).
- Backup story is one more cron job.
- No DBA worries.
- If the system outgrows sqlite (unlikely at this scale), the same schemas port to Postgres with minimal changes.

Trade-offs:

- sqlite has gotchas at high concurrency (writer serialization). Acceptable for our workloads.
- No native replication — if we ever need multi-host state, we re-open this ADR.

## Links

- [[13-decisions/0010-single-host-v1]]
- [[06-ingestion/lifecycle-engine]]
- [[09-operations/asset-matrix]]
- [[11-migration/scaling]]
