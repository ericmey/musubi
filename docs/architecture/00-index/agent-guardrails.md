---
title: Agent Guardrails
section: 00-index
tags: [agents, contributing, guardrails, section/index, status/complete, type/index]
audience: coding-agents
type: index
status: complete
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: false
---
# Agent Guardrails — Rules for Coding Agents

This vault will be worked on by a fleet of coding agents in parallel. This document is the **contract** between them. Violating these rules produces merge conflicts, duplicated abstractions, and drift. Read this first, every time.

> **Agent onboarding path:** start at [[CLAUDE|CLAUDE.md]] (the entry point), then this file, then [[00-index/agent-handoff]], then your slice file in [[_slices/index|_slices/]]. The section your slice touches has a local `CLAUDE.md` (e.g. `04-data-model/CLAUDE.md`) — read that before editing any file in that section.

## The four non-negotiables

1. **Stay inside your slice.** Every slice has its own note in [[_slices/index|_slices/]] with an explicit `slice_id`, `owns_paths` list, and `forbidden_paths` list. You may read anywhere. You may only *write* to files under `owns_paths`. If you need to change a file outside your slice, **open a cross-slice ticket** (create a markdown file in `_inbox/cross-slice/<slice-id>-<target>.md`) and flip your slice to `blocked` until a human or meta-agent resolves it.
2. **The canonical API is frozen per version.** If your slice is not `slice=api-v*`, you do not modify `musubi/api/` or the OpenAPI/proto files. Additive changes (new optional fields, new endpoints) require an ADR; breaking changes bump the version.
3. **Every module has a test contract. Write tests first.** The spec in `04-data-model`, `05-retrieval`, `06-ingestion`, etc. contains a **Test Contract** section. Your first commit in a slice must be the test file realizing that contract. Your PR is not mergeable until the contract tests pass AND branch coverage on your owned files is ≥ 85%.
4. **Do not silently rebase the vault.** This documentation vault is versioned. If your implementation forces a spec change, update the spec file **in the same PR** as the code change and tag the commit with `spec-update: <doc-path>`.

## Slice boundaries

The repo is partitioned into ownership zones. See [[12-roadmap/ownership-matrix]] for the full matrix. High level:

| Zone | Path | Who may write |
|---|---|---|
| **Core types** | `musubi/types/`, `musubi/schema/` | Only `slice=types` agents |
| **Planes** | `musubi/planes/episodic/`, `musubi/planes/curated/`, `musubi/planes/artifact/` | Plane-specific slice agents only |
| **Retrieval** | `musubi/retrieval/` | `slice=retrieval-*` agents |
| **Lifecycle engine** | `musubi/lifecycle/` | `slice=lifecycle-*` agents |
| **Canonical API** | `musubi/api/`, `openapi.yaml`, `proto/` | Only `slice=api-v*` agents, one at a time |
| **SDK** | separate repo `musubi-sdk-py`, `musubi-sdk-ts` | SDK slice agents |
| **Adapters** | separate repos `musubi-mcp`, `musubi-livekit`, `musubi-openclaw` | Adapter slice agents |
| **Deployment** | `deploy/ansible/` | `slice=ops-*` agents |
| **Docs** | `docs/musubi-architecture/` | Any agent for their owned slice's docs; cross-cutting doc changes require a meta-agent |

## Locking and coordination

- **One agent per module per slice.** Use a trivial file-lock pattern: before starting work, create `docs/musubi-architecture/_inbox/locks/<module-path>.lock` containing your agent ID and start timestamp. Remove it on PR open. If a lock exists and is > 4h old, it is stale — any agent may delete it.
- **Long-running agents must heartbeat.** Update the `.lock` file timestamp every 30 minutes. This is how stale-detection works.
- **PR size cap: 800 LOC** (excluding generated code and fixtures). Bigger slices must be subdivided in the roadmap before starting.

## Style and conventions

- **Python:** black-compatible, ruff linted, mypy strict. No exceptions.
- **Types:** every public function has a type hint. Every payload is a pydantic model, not a dict. Dicts are only at the Qdrant boundary.
- **Error handling:** every public function returns a `Result[T, Error]` (either a typed error dataclass or a success). No raising across module boundaries. Unhandled exceptions get caught at the API layer and converted to 5xx with a correlation ID.
- **Async vs sync:** the public surface is async. Internal worker loops may be sync if they don't touch I/O.
- **Logging:** structured JSON logs, one field per concept. No f-strings in log messages (use `logger.info("event", extra={...})`). Correlation IDs propagate.
- **No `print()`** anywhere. Ever.
- **Comments explain *why*, not *what*.** If you need to explain what a function does, the function is named wrong.

## Qdrant rules (specific gotchas)

- Never loop `set_payload`. Use `batch_update_points` with `SetPayloadOperation`. This has been a recurring N+1 source in the POC.
- Never filter Qdrant results in Python. Every filter you'd write in a list comprehension can live in the Qdrant query as a `must` / `must_not` / `should`. Put it there.
- Every Qdrant call is wrapped in try/except. Returns `Err(QdrantError(...))` on failure, not an exception.
- Use **named vectors** from day one for any new collection. Even if you only have `dense_v1`, creating a named vector now avoids a migration later when you add `sparse` or `dense_v2`.

## Obsidian vault rules

- You may read any file in the vault.
- You may **only write** to files whose frontmatter has `musubi-managed: true` **AND** your slice is authorized in [[06-ingestion/vault-sync#write-authorization]].
- **Never** modify files in `_inbox/` programmatically — that folder is human-only except for the agent-created ticket pattern described above.
- All programmatic vault writes go through the `MusubiVault.write()` API, which handles debouncing, rename atomicity, and frontmatter schema validation.

## Escalation

If you're blocked, unsure, or notice a contradiction in the spec:

1. Don't guess. Don't "just make it work."
2. Create `docs/musubi-architecture/_inbox/questions/<slice-id>-<short-title>.md` with: what you're trying to do, what you expected, what you observed, what options you see.
3. Mark your slice status as `blocked` in [[12-roadmap/status]].
4. Move on to another slice you own.

## Definition of Done for a slice

A slice is done when all are true:

- [ ] All test contracts for owned modules pass.
- [ ] Branch coverage ≥ 85% on owned files, ≥ 70% on touched files.
- [ ] `make check` passes clean (format, lint, mypy, tests).
- [ ] Docs in the corresponding `docs/musubi-architecture/<section>/` are updated to reflect what was built.
- [ ] If any spec file changed, PR commit is tagged `spec-update: <path>`.
- [ ] A human has reviewed and merged the PR.
- [ ] The slice's entry in [[12-roadmap/status]] is marked `done`.

## Prohibited patterns (automatic revert)

- Silent `time.sleep()` in production code paths (use async waits with timeouts).
- Environment-variable reads outside of `musubi/config.py`.
- Hardcoded hosts, ports, collection names, or thresholds.
- New top-level dependencies without an ADR.
- Mutating shared global state without a lock.
- `except Exception: pass`.
