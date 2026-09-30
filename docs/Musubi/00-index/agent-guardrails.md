---
title: "Agent Guardrails — Rules for Coding Agents"
section: 00-index
tags: [agents, contributing, guardrails, section/index, status/complete, type/index]
audience: coding-agents
type: index
status: complete
updated: 2026-09-30
up: "[[00-index/index]]"
reviewed: true
---

# Agent Guardrails

The contract for every coding agent and contributor, including the non-negotiables, the Test Contract
Closure Rule, review and the prohibited patterns, is **[AGENTS.md](../../../AGENTS.md)** at the repo
root. This page keeps only the rules specific to Qdrant and to this vault, which AGENTS.md doesn't
repeat.

## Qdrant rules (specific gotchas)

- Never loop `set_payload`. Use `batch_update_points` with `SetPayloadOperation`. This has been a recurring N+1 source in the POC.
- Never filter Qdrant results in Python. Every filter you'd write in a list comprehension can live in the Qdrant query as a `must` / `must_not` / `should`. Put it there.
- Every Qdrant call is wrapped in try/except. Returns `Err(QdrantError(...))` on failure, not an exception.
- Use **named vectors** from day one for any new collection. Even if you only have `dense_v1`, creating a named vector now avoids a migration later when you add `sparse` or `dense_v2`.

## Obsidian vault rules

- You may read any file under `docs/Musubi/`.
- Programmatic writes to vault-managed knowledge notes (curated plane) go through the `MusubiVault.write()` API, which handles debouncing, rename atomicity, and frontmatter schema validation — see [[06-ingestion/vault-sync]].
- Spec and ADR edits are a normal code-review change — commit them with the PR that motivated them, tagged `spec-update: <doc-path>` in the trailer.

## When you're stuck

Don't guess and don't "just make it work". Comment on the issue or draft PR with the goal, what you
expected, what you observed and the options you see, then ask. (The old `_inbox/questions/` files
and slice `blocked` status were retired with the slice workflow on 2026-09-30.)
