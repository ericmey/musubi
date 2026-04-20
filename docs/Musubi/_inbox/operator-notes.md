---
title: Operator Notes
section: _inbox
type: scratchpad
status: living-document
tags: [type/scratchpad, status/living-document]
updated: 2026-04-17
reviewed: true
---

# Operator Notes

Eric's scratch pad while reviewing the vault. No structure required. Drop anything here: questions, reactions, "that's wrong", "why this and not X", "add example for Y".

The **`[R]`** task marker means *research / question* — it gets picked up automatically by the [[00-index/research-questions]] live query and the [[_inbox/research/research-board|research board]].

The **`[/]`** marker is in-progress. **`[x]`** is resolved.

## Open

- [R] _Example:_ why do we use KSUID instead of UUIDv7? [[00-index/conventions]] says "not UUIDs" — is this a hard constraint or a preference?

### 2026-04-20 — deploy-readiness findings (from ansible dry-run against `<musubi-host>`)

- [R] **Ollama model drift.** `deploy/ansible/group_vars/all.yml` pins `musubi_ollama_model: "qwen3:4b"` but the pre-staged native install on the host has `qwen2.5:7b-instruct-q4_K_M` (~4.7 GB, confirmed via `curl /api/tags` on 2026-04-20). Decide which is authoritative. [[13-decisions/0019-qwen-on-musubi-gpu-phase-1]] chose Qwen 3 4B; the host's pre-stage missed that bump. Either re-pull 3:4b and discard 2.5:7b, or update the ADR + group_vars to Qwen 2.5:7b.
- [R] **Health-check target mismatch.** `deploy/ansible/health.yml` probes `http://127.0.0.1:6333/health` (Qdrant) and `http://127.0.0.1:11434/api/tags` (Ollama). [[13-decisions/0014-kong-over-caddy]] §Decision says inference services are "bridge-only inside Docker Compose. No host ports, no LAN access." These contradict each other — either (a) the compose file DOES publish Qdrant/Ollama on host 127.0.0.1 (and ADR 0014's phrasing is aspirational), or (b) `health.yml` needs to shell through `docker compose exec <svc>` to check. Decide and align.

## In progress

## Resolved

## Gut reactions / questions as I read

Free-form below here — headings are optional. Just date-stamp each block if you care.

### 2026-04-17 — first pass

-

## How this file works

- Drop questions as they occur. Use `[R]` to make them searchable.
- At end of a review session, triage: promote strong ones to their own file in `_inbox/research/` via the **Research Question** template.
- Weak ones stay here as working memory.
- Nothing here is load-bearing — this is your notebook.
