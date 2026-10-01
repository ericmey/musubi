---
title: Agent Guardrails
section: 00-index
tags: [agents, contributing, guardrails, section/index, status/complete, type/index]
audience: coding-agents
type: index
status: complete
updated: 2026-10-01
up: "[[00-index/index]]"
reviewed: true
---

# Agent Guardrails

The contract for every coding agent and contributor, including the non-negotiables, the Test Contract
Closure Rule, review and the prohibited patterns, is **[AGENTS.md](../../../AGENTS.md)** at the repo
root. This page keeps only the rules specific to Qdrant and to this vault, which AGENTS.md doesn't
repeat.

## Qdrant rules (specific gotchas)

- Never loop `set_payload`. Use `batch_update_points` with `SetPayloadOperation`. This was a recurring N+1 source before v1.
- Never filter Qdrant results in Python. Every filter you'd write in a list comprehension can live in the Qdrant query as a `must` / `must_not` / `should`. Put it there.
- Every Qdrant call is wrapped in try/except and returns an `Err(...)` (`Result` type, `src/musubi/types/common.py`) on failure at the module boundary, not a raw exception.
- Use **named vectors** from day one for any new collection. Even if you only have `dense_v1`, creating a named vector now avoids a migration later when you add `sparse` or `dense_v2`.

## Obsidian vault rules

- You may read any file under `docs/Musubi/`.
- Programmatic writes to curated notes in the knowledge vault (`VAULT_PATH`, not this docs folder) go through `VaultWriter.write_curated()` in `src/musubi/vault/writer.py`. It takes a validated `CuratedFrontmatter`, records the write in the write log so Musubi's own echo is ignored, refuses paths outside the vault root and writes atomically (temp file, then rename). See [[06-ingestion/vault-sync]].
- Spec and ADR edits are a normal code-review change — commit them with the PR that motivated them, tagged `spec-update: <doc-path>` in the trailer.

## When you're stuck

Don't guess and don't "just make it work". Comment on the issue or draft PR with the goal, what you
expected, what you observed and the options you see, then ask. (The old `_inbox/questions/` files
and slice `blocked` status were retired with the slice workflow on 2026-09-30.)
