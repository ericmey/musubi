---
title: "06 — Ingestion & Lifecycle"
section: 06-ingestion
tags: [ingestion, lifecycle, maturation, promotion, section/ingestion, status/stub, synthesis, type/spec]
type: spec
status: stub
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# 06 — Ingestion & Lifecycle

How memory enters the system, ripens, gets synthesized into concepts, and gets promoted to curated. The write-side of Musubi.

## Documents in this section

- [[06-ingestion/capture]] — The capture API. What shape comes in, what validation, what writes.
- [[06-ingestion/maturation]] — Provisional → matured. Hourly sweep. Importance scoring + tag normalization.
- [[06-ingestion/concept-synthesis]] — Daily clustering of matured memories into concepts.
- [[06-ingestion/promotion]] — Concept → curated knowledge. Gating rules + vault write path.
- [[06-ingestion/demotion]] — Matured → demoted. Decay rules by plane.
- [[06-ingestion/vault-sync]] — Filesystem watcher. Human edit → Qdrant re-index.
- [[06-ingestion/lifecycle-engine]] — The Lifecycle Worker process. Schedule, concurrency, idempotency.
- [[06-ingestion/reflection]] — Daily reflection digest. Surfaces patterns; writes to `vault/reflections/`.
- [[06-ingestion/embedding-strategy]] — What we embed, when, with what model, and how we avoid re-embedding churn.
- [[06-ingestion/vault-frontmatter-schema]] — The YAML schema enforced on human-edited curated files.

## Two write surfaces

1. **API write** — `POST /v1/episodic`, `POST /v1/artifacts`, etc. Used by adapters (Claude Code, LiveKit, OpenClaw). Documented in [[07-interfaces/canonical-api]].
2. **Vault write** — a human saves a markdown file in Obsidian. Picked up by the Vault Watcher. Documented in [[06-ingestion/vault-sync]].

Everything else — maturation, synthesis, promotion, demotion, reflection — is a **background process** run by the Lifecycle Worker, not a user-facing write path. See [[06-ingestion/lifecycle-engine]].

## Principles

1. **Hot path is thin.** API writes do the minimum: validate, compute embedding, upsert, respond. Enrichment happens later, not in the response path.
2. **Maturation is not magic.** Every provisional memory earns its way to matured via a documented rule. See [[06-ingestion/maturation]].
3. **Synthesis is LLM-assisted.** But the LLM never writes directly to the index — it emits a structured proposal that a deterministic Python path validates and stores.
4. **Promotion is always auditable.** Every promotion produces a LifecycleEvent + a Thought to the operator. See [[04-data-model/lifecycle]] and [[06-ingestion/promotion]].
5. **Nothing is deleted silently.** `demoted`, `archived`, `superseded` are first-class states. Hard deletes require operator scope.
6. **Vault writes and index writes are reconcilable.** If they drift (index-only or vault-only state), a reconciler job brings them back. See [[09-operations/asset-matrix]].

## Schedule at a glance

| Job | Frequency | Runs in |
|---|---|---|
| Maturation sweep (episodic) | Hourly | Lifecycle Worker |
| Provisional TTL (7d → archived) | Hourly | Lifecycle Worker |
| Concept synthesis | Daily (03:00 local) | Lifecycle Worker |
| Concept maturation (24h after synth) | Daily | Lifecycle Worker |
| Concept demotion | Daily | Lifecycle Worker |
| Promotion sweep | Daily | Lifecycle Worker |
| Reflection digest | Daily (06:00 local) | Lifecycle Worker |
| Episodic demotion (weekly) | Weekly (Sunday 03:00) | Lifecycle Worker |
| Vault reconciler | Every 6h | Lifecycle Worker |

All schedules are tunables; defaults optimized for a household cadence (humans look at it once a day; system digests over 24h windows).

## Ownership

- Lifecycle logic lives in `musubi/lifecycle/*`.
- Each job has its own file: `maturation.py`, `synthesis.py`, `promotion.py`, `demotion.py`, `reflection.py`, `reconcile.py`.
- The worker process (`musubi-lifecycle`) loads and schedules them via APScheduler + a file-based lock to prevent double-execution.

Tests for each job live in `tests/lifecycle/test_<job>.py`. See [[00-index/test-index]].
