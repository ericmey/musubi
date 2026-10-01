---
title: "Agent Rules — Ingestion & Lifecycle (06)"
section: 06-ingestion
type: index
status: complete
tags: [section/ingestion, status/complete, type/index, agents]
updated: 2026-10-01
up: "[[06-ingestion/index]]"
reviewed: true
---

# Agent Rules — Ingestion & Lifecycle (06)

Local rules for changes under `src/musubi/ingestion/`, `src/musubi/lifecycle/` and `src/musubi/vault/`. Supplements [[CLAUDE]].

## Must

- **No silent mutation.** Every state change goes through `transition()` (`src/musubi/lifecycle/transitions.py`) and records a `LifecycleEvent`. See [[13-decisions/0007-no-silent-mutation]].
- **Idempotent jobs.** Maturation, synthesis, promotion, demotion, reflection and reconcile must be safe to re-run over the same window. Missed cron occurrences are not replayed, so a job must resume from its cursor or re-select by state.
- **One vault writer.** `VaultWriter` (`src/musubi/vault/writer.py`) is the only path that writes the vault. It records the write-log entry before writing the file. No direct file I/O on the vault from other modules.
- **Echo-filter vault writes.** The watcher must not re-ingest a file Musubi just wrote. Use the write-log; see [[06-ingestion/vault-sync#Echo prevention]].
- **Validate frontmatter on every vault read.** Skip and log files that fail `CuratedFrontmatter`; never crash the watcher or the reconciler.
- **Take the job lock.** Every scheduled job wraps its sweep in `file_lock(lock_dir / "<job>.lock")` and skips when the lock is held.

## Must not

- Delete memory objects on demotion. Demotion is a state transition (`demoted` / `archived`), never a delete.
- Run synthesis on provisional episodics. Only `matured` rows feed synthesis.
- Write a promotion into the vault except through `VaultWriter`, which records the write-log first.
- Add a scheduler dependency or a sleep-based scheduler. The worker is the tick loop in `src/musubi/lifecycle/runner.py`, and new jobs register a `Job` with a cron or interval trigger.

## Job cadences (don't change without updating ops)

Times are UTC. Locks live in `<parent of LIFECYCLE_SQLITE_PATH>/locks/`.

| Job | Cadence | Lock file |
|---|---|---|
| `maturation_episodic` | hourly at :13 | `maturation_episodic.lock` |
| `provisional_ttl` | hourly at :17 | `provisional_ttl.lock` |
| `synthesis` | daily 03:00 | `synthesis.lock` |
| `concept_maturation` | daily 03:30 | `concept_maturation.lock` |
| `promotion` | daily 04:00 | `promotion.lock` |
| `demotion_concept` | daily 05:00 | `demotion_concept.lock` |
| `demotion_episodic` | Sundays 03:45 | `demotion_episodic.lock` |
| `demotion_artifact` | Sundays 04:15 (opt-in) | `demotion_artifact.lock` |
| `reflection_digest` | daily 06:00 | `reflection.lock` |
| `vault_reconcile` | every 6 h | `vault_reconcile.lock` |
| `lifecycle_reconcile` | every `LIFECYCLE_RECONCILE_INTERVAL_S` (5 s) | none (coordinator leases) |

The schedule is defined in `src/musubi/lifecycle/scheduler.py` and the per-job builders. Changing a cadence is a code change: update [[06-ingestion/lifecycle-engine]] and [[09-operations/runbooks]] with it.

## When an LLM is in the loop

- **The model is deployment-selected** (ADR 0043, [[13-decisions/0043-lifecycle-llm-openai-compatible-endpoint]]). Maturation and synthesis use the lifecycle LLM settings: `LIFECYCLE_LLM_API` (`ollama` or `openai`), `LIFECYCLE_LLM_BASE_URL`, `LIFECYCLE_LLM_MODEL`, `LIFECYCLE_LLM_API_KEY`, falling back to `OLLAMA_URL` / `LLM_MODEL`. Promotion and reflection currently call `OLLAMA_URL` with `LLM_MODEL` directly. Do not hard-code a model or a provider.
- **Prompt versions are frozen.** Prompts live in `src/musubi/llm/prompts/<name>/v<N>.txt`. A prompt change is a new file, never an edit.
- **Validate every LLM output** against a pydantic model before use. Treat memory content as untrusted data in prompts (`src/musubi/llm/prompt_boundary.py`).
- **Degrade, don't crash.** An LLM failure falls back (maturation), skips the item (synthesis, promotion) or writes a skip notice (reflection). It never fails the whole job.
