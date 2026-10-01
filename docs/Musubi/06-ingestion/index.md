---
title: "06 — Ingestion & Lifecycle"
section: 06-ingestion
tags: [ingestion, lifecycle, maturation, promotion, section/ingestion, status/stub, synthesis, type/spec]
type: spec
status: stub
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: false
---
# 06 — Ingestion & Lifecycle

How memory enters the system, ripens, gets synthesized into concepts and gets promoted to curated knowledge. This is the write side of Musubi.

## Documents in this section

- [[06-ingestion/capture]]: the capture API. What comes in, how it is validated, what gets written.
- [[06-ingestion/maturation]]: provisional to matured. Hourly sweep, importance scoring, tag normalization.
- [[06-ingestion/concept-synthesis]]: daily clustering of matured memories into concepts.
- [[06-ingestion/promotion]]: concept to curated knowledge. Gating rules and the vault write path.
- [[06-ingestion/demotion]]: matured to demoted. Decay rules by plane.
- [[06-ingestion/vault-sync]]: vault reconciler and optional filesystem watcher. Human edits reach the index.
- [[06-ingestion/lifecycle-engine]]: the lifecycle worker process. Schedule, locks, failure handling.
- [[06-ingestion/reflection]]: daily reflection digest written to `vault/reflections/`.
- [[06-ingestion/embedding-strategy]]: what is embedded, when, with which model, and how re-embedding churn is avoided.
- [[06-ingestion/vault-frontmatter-schema]]: the YAML schema enforced on curated files.
- [[06-ingestion/life009-semantic-supersession]]: how maturation infers supersession.

## Two write surfaces

1. **API write:** `POST /v1/episodic`, `POST /v1/artifacts`, `POST /v1/curated` and so on. Used by the SDK, the in-repo MCP adapter and client integrations (maintained in sibling repositories). See [[07-interfaces/canonical-api]].
2. **Vault write:** a person saves a markdown file in the vault. The 6-hourly reconciler (and the watcher, if running) picks it up. See [[06-ingestion/vault-sync]].

Everything else (maturation, synthesis, promotion, demotion, reflection) is a **background job** in the lifecycle worker, not a user-facing write path. See [[06-ingestion/lifecycle-engine]].

## Principles

1. **The hot path is thin.** API writes validate, embed, dedup, write and respond. Enrichment happens later.
2. **Maturation is not magic.** Every provisional memory earns its way to matured by a documented rule. See [[06-ingestion/maturation]].
3. **Synthesis is LLM-assisted, not LLM-written.** The LLM returns a structured proposal; deterministic Python validates and stores it.
4. **Promotion is auditable.** Every promotion records a LifecycleEvent and sends an ops-alerts Thought. See [[04-data-model/lifecycle]] and [[06-ingestion/promotion]].
5. **Nothing is deleted silently.** `demoted`, `archived` and `superseded` are first-class states. Hard deletes require operator scope.
6. **Vault and index are reconcilable.** If they drift, the `vault_reconcile` job brings them back. See [[09-operations/asset-matrix]].

## Schedule at a glance

All jobs run in the lifecycle worker. Times are UTC.

| Job | Frequency |
|---|---|
| Maturation sweep (episodic) | hourly at :13 |
| Provisional TTL (7 days, then archived) | hourly at :17 |
| Concept synthesis | daily 03:00 |
| Concept maturation (24 h after synthesis) | daily 03:30 |
| Promotion sweep | daily 04:00 |
| Concept demotion | daily 05:00 |
| Reflection digest | daily 06:00 |
| Episodic demotion | weekly, Sunday 03:45 |
| Artifact archival (opt-in) | weekly, Sunday 04:15 |
| Vault reconciler | every 6 h |
| Lifecycle reconcile (transition outbox) | every 5 s (`LIFECYCLE_RECONCILE_INTERVAL_S`) |

The schedule is fixed in code and tuned for a small-team cadence: people look once a day, and digests cover 24-hour windows.

## Ownership

- Lifecycle logic lives in `src/musubi/lifecycle/`, one module per job family: `maturation.py` (episodic maturation, provisional TTL, concept maturation), `synthesis.py`, `promotion.py`, `demotion.py`, `reflection.py`. Vault reconcile is in `src/musubi/vault/reconciler.py`.
- The worker process is the `lifecycle-worker` Compose service, `python -m musubi.lifecycle.runner`: a tick loop that dispatches each job, with a per-job `flock` to prevent double execution.

Tests for each job live in `tests/lifecycle/test_<job>.py` and `tests/vault/`. See [[00-index/test-index]].
