---
title: "Agent Rules — Ingestion & Lifecycle (06)"
section: 06-ingestion
type: index
status: complete
tags: [section/ingestion, status/complete, type/index, agents]
updated: 2026-04-17
up: "[[06-ingestion/index]]"
reviewed: true
---

# Agent Rules — Ingestion & Lifecycle (06)

Local rules for slices under `musubi/ingestion/`, `musubi/lifecycle/`, `musubi/vault_sync/`. Supplements [[CLAUDE]].

## Must

- **No silent mutation.** Every state change emits a `LifecycleEvent`. See [[13-decisions/0007-no-silent-mutation]].
- **Idempotent jobs.** Maturation, synthesis, promotion, reflection must be safe to re-run on the same window. Use `last_processed_at` + `job_run_id`.
- **One serialized vault writer.** `MusubiVault.write()` is the only path that mutates the Obsidian vault. No direct file I/O on `vault/`.
- **Echo-filter vault writes.** The watcher must not re-ingest a file Musubi itself just wrote. Use the write-log; see [[06-ingestion/vault-sync#echo filter]].
- **Frontmatter validation on every vault read.** Reject or quarantine files that fail schema; never crash the watcher.

## Must not

- Delete memory objects on demotion. Demotion is a soft-delete flag + move to `_archive/`.
- Run synthesis on provisional episodics. Only `matured` objects feed synthesis.
- Write a promotion straight into `vault/` without the write-log pre-record.
- Use `time.sleep()` in the scheduler — use APScheduler's own timing primitives.

## Job cadences (don't change without updating ops)

| Job               | Default cadence | Lock path                           |
|-------------------|-----------------|-------------------------------------|
| Maturation        | hourly          | `/srv/musubi/locks/maturation.lock` |
| Concept synthesis | daily, 03:00    | `/srv/musubi/locks/synthesis-<ns>.lock` |
| Promotion         | daily, 04:00    | `/srv/musubi/locks/promotion.lock`  |
| Reflection        | daily, 05:00    | `/srv/musubi/locks/reflection.lock` |
| Vault reconciler  | every 5m        | `/srv/musubi/locks/vault-reconcile.lock` |

Changing cadence requires an ADR and updates to [[09-operations/runbooks]].

## When an LLM is in the loop

- Use Qwen2.5-7B via Ollama locally by default. Gemini is the optional fallback.
- **Prompt versions are frozen per ADR.** Store prompts in `musubi/llm/prompts/<name>/v<N>.txt`. A prompt change is a new file, never an edit.
- Every LLM output is validated against a pydantic model before use.

## Related slices

- [[_slices/slice-ingestion-capture]], [[_slices/slice-vault-sync]], [[_slices/slice-embedding]].
- [[_slices/slice-lifecycle-engine]], [[_slices/slice-lifecycle-maturation]], [[_slices/slice-lifecycle-synthesis]], [[_slices/slice-lifecycle-promotion]], [[_slices/slice-lifecycle-reflection]].
