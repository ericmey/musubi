---
title: "Agent Guardrails — Rules for Coding Agents"
section: 00-index
tags: [agents, contributing, guardrails, section/index, status/complete, type/index]
audience: coding-agents
type: index
status: complete
updated: 2026-04-18
up: "[[00-index/index]]"
reviewed: true
---
# Agent Guardrails — Rules for Coding Agents

This repo is worked on by a fleet of coding agents (Claude Code, Claude Cowork, Codex, Cursor, Gemini, Grok, …) in parallel. This document is the **contract** between them. Violating these rules produces merge conflicts, duplicated abstractions, silent scope reduction, and drift. Read this first, every time.

> **Agent onboarding path:** start at `CLAUDE.md` at the repo root (the entry point), then `docs/AGENT-PROCESS.md` (multi-agent concurrency model), then this file, then [[00-index/agent-handoff]], then your slice file in [[_slices/index|_slices/]]. The section your slice touches has a local `CLAUDE.md` (e.g. `04-data-model/CLAUDE.md`) — read that before editing any file in that section.

## The four non-negotiables

1. **Stay inside your slice.** Every slice has its own note in [[_slices/index|_slices/]] with an explicit `slice_id`, `owns_paths` list, and `forbidden_paths` list. You may read anywhere. You may only *write* to files under `owns_paths`. If you need to change a file outside your slice, **open a cross-slice ticket** (a markdown file in `docs/Musubi/_inbox/cross-slice/<slice-id>-<target>.md` plus a GitHub Issue using the `cross-slice` template) and flip your slice to `blocked` until a human or meta-agent resolves it.
2. **The canonical API is frozen per version.** If your slice is not `slice-api-v*`, you do not modify `src/musubi/api/`, `openapi.yaml`, or `proto/`. Additive changes (new optional fields, new endpoints) require an ADR; breaking changes bump the version.
3. **Every spec has a Test Contract. Write tests first.** The spec in `04-data-model/`, `05-retrieval/`, `06-ingestion/`, etc. contains a **Test Contract** section. Your first commit in a slice is the test file realising that contract. Your PR is not mergeable until the contract tests pass AND branch coverage on your owned files is ≥ 85 % (90 % for `src/musubi/planes/**` and `src/musubi/retrieve/**`).
4. **Do not silently rebase the spec.** If your implementation forces a spec change, update the spec file **in the same PR** as the code change and tag the commit with `spec-update: <doc-path>` in the trailer.

## Test Contract Closure Rule

At handoff, every bullet in the spec's `## Test Contract` section is in **exactly one of three closure states**:

1. **Passing test** — a function in `tests/` whose name transcribes the bullet text verbatim (with `_` for spaces). Running `pytest` shows it green.
2. **Skipped test with reason** — `@pytest.mark.skip(reason="deferred to slice-<id>: <one-line-why>")` or `@pytest.mark.xfail(reason="...")`. The skip reason must name the follow-up slice **and** justify the deferral.
3. **Declared out-of-scope in the slice's work log** — an entry under `## Work log` in `_slices/<slice-id>.md` naming the bullet, the reason it's deferred, and the follow-up home. A GitHub Issue for the follow-up must exist.

**Silent omission is not one of the three states.** A spec Test Contract bullet with no passing test, no skipped-with-reason test, and no work-log entry is an **automatic review request-changes**. The reviewer agent (`musubi-reviewer`) surfaces silent omissions as a Must-fix in the PR review.

Rationale: on 2026-04-18 a slice first-cut deferred `patch()` + `delete()` + `access_count` to downstream slices silently — some correctly scoped, some not. The result was that other agents reading the spec could not tell which bullets had been consciously punted vs. overlooked. This rule closes that loophole.

## Method-ownership rule

**If the method's code would live inside your `owns_paths`, you own the method.**

Corollary: you may **not** defer a method to a slice that merely exposes it through a different surface. Example pattern: `EpisodicPlane.patch()` is owned by the plane slice (its code lives in `src/musubi/planes/episodic/`). The API slice (`slice-api-v0`) exposes that method via `PATCH /v1/episodic-memories/{id}` but does not own the implementation. Pushing `patch()` onto slice-api-v0 mis-scopes the work.

When you believe a method belongs to a different slice, the test is mechanical: would that slice's `owns_paths` list contain the file the method lives in? If yes, defer. If no, you own it.

## Dual-update rule (vault frontmatter ↔ GitHub Issue)

**Every slice state change updates both the vault file AND the Issue, atomically, in the same PR.**

The GitHub Issue is the authoritative lock (per [docs/AGENT-PROCESS.md](../../docs/AGENT-PROCESS.md) — assignee is atomic across machines). The slice file's frontmatter is the authoritative *intent* record (audited, diffable, reviewed in PRs, read in Obsidian). They must not be allowed to drift.

The three state changes, with the required commands for each:

### Claim (ready → in-progress)

```bash
# Atomic claim on the Issue:
gh issue edit <n> --add-assignee @me \
  --add-label "status:in-progress" --remove-label "status:ready"

# Same PR — update the slice note's frontmatter:
# status: ready → in-progress
# owner: unassigned → <your-agent-id>
```

### Handoff (in-progress → in-review)

```bash
# Update the Issue:
gh issue edit <n> \
  --add-label "status:in-review" --remove-label "status:in-progress"

# Same PR — update the slice note:
# status: in-progress → in-review
```

### Merge (in-review → done)

Handled by the PR auto-closing the Issue via `Closes #<n>` in its body. The merging agent (or a follow-up PR) flips the slice frontmatter `status: in-review → done` and the `status:done` label is applied automatically by the follow-up sync Action (or by hand, for now).

### Block (any → blocked)

```bash
# Update the Issue:
gh issue edit <n> --add-label "status:blocked"
gh issue comment <n> --body "Blocked on <reason + link to cross-slice ticket or question>"

# Same PR — update the slice note:
# status: <previous> → blocked
# append a work-log entry naming the blocker
```

### Enforcement

- **`make issue-check`** (or `make agent-check`, which includes it) cross-references slice frontmatter against Issue labels and reports any drift. It's PR-blocking as a warning in review; agents must resolve drift before handoff.
- **`musubi-reviewer` sub-agent** treats dual-update violations as an automatic request-changes.
- **Renames / splits / retires** go through the `slice-reconcile` skill (see `.claude/skills/` and `.agents/skills/`), which walks the agent through updating both the vault files and the Issues coherently. Manual edits to one side without the other are a merge-blocker.

### What's NOT automated (yet)

- Bidirectional sync between free-form Issue body text and slice note body — write-once on Issue creation, diverge freely thereafter.
- Work-log mirroring into Issue comments — the vault is the single narrative record. Issue comments are for real-time coordination.
- Automatic creation of an Issue when a new slice file is added — the `bootstrap_slice_issues.py` tool handles this, but it's manually invoked (`python3 docs/Musubi/_tools/bootstrap_slice_issues.py --apply`). Skipped intentionally; running it automatically could race with PR reviews that haven't merged yet.

## Slice boundaries

The repo is partitioned into ownership zones. See [[12-roadmap/ownership-matrix]] for the full matrix. High level (under the monorepo layout — see [[13-decisions/0015-monorepo-supersedes-multi-repo]] and [[13-decisions/0016-vault-in-monorepo]]):

| Zone | Path | Who may write |
|---|---|---|
| **Core types** | `src/musubi/types/` | Only `slice-types` agents |
| **Planes** | `src/musubi/planes/{episodic,curated,artifact,concept,thoughts}/` | Plane-specific slice agents only |
| **Retrieval** | `src/musubi/retrieve/` | `slice-retrieval-*` agents |
| **Lifecycle engine** | `src/musubi/lifecycle/` | `slice-lifecycle-*` agents |
| **Canonical API** | `src/musubi/api/`, `openapi.yaml`, `proto/` | Only `slice-api-v*` agents, one at a time |
| **SDK** | `src/musubi/sdk/` | `slice-sdk-*` agents |
| **Adapters** | `src/musubi/adapters/{mcp,obsidian,cli}/` | Adapter slice agents |
| **Contract tests** | `src/musubi/contract_tests/` | `slice-contract-tests` agent |
| **Deployment** | `deploy/` | `slice-ops-*` agents |
| **Architecture vault** | `docs/Musubi/` | Any agent for their slice's specs; cross-cutting changes require a meta-agent or `spec-update:` trailer in the same PR |

## Locking and coordination

Primary lock is a **GitHub Issue** with the `slice` label and the agent as assignee. The Issue's labels reflect slice status (`status:ready` / `status:in-progress` / `status:in-review` / `status:blocked` / `status:done`). See [docs/AGENT-PROCESS.md](../../docs/AGENT-PROCESS.md) for the full concurrency model.

Secondary (belt-and-braces) lock: create `docs/Musubi/_inbox/locks/<slice-id>.lock` containing your agent ID and start timestamp. Remove it when the PR is marked ready-for-review. If a lock is > 4 h old with no corresponding commits on `slice/<id>`, it is stale — any agent may delete it after commenting on the Issue.

**PR size cap: 800 LOC** (excluding generated code and fixtures). Bigger slices must be subdivided in the roadmap before starting.

## Style and conventions

- **Python:** black-compatible via ruff. mypy strict. No exceptions.
- **Types:** every public function has a type hint. Every payload is a pydantic v2 model, not a dict. Dicts are only at the Qdrant boundary.
- **Error handling:** public functions at module boundaries return `Result[T, E]` with a typed error dataclass — not raised exceptions. Unhandled exceptions get caught at the API layer and converted to 5xx with a correlation ID.
- **Async vs sync:** public surface is async. Internal worker loops may be sync if they don't touch I/O.
- **Logging:** structured JSON logs, one field per concept. No f-strings in log messages (use `logger.info("event", extra={...})`). Correlation IDs propagate.
- **No `print()`** anywhere. Ever.
- **Comments explain *why*, not *what*.** If you need to explain what a function does, the function is named wrong.
- **Test function names transcribe the spec's Test Contract bullet verbatim** (with `_` for spaces). See [[00-index/conventions#Test-name convention]].

## Qdrant rules (specific gotchas)

- Never loop `set_payload`. Use `batch_update_points` with `SetPayloadOperation`. This has been a recurring N+1 source in the POC.
- Never filter Qdrant results in Python. Every filter you'd write in a list comprehension can live in the Qdrant query as a `must` / `must_not` / `should`. Put it there.
- Every Qdrant call is wrapped in try/except. Returns `Err(QdrantError(...))` on failure, not an exception.
- Use **named vectors** from day one for any new collection. Even if you only have `dense_v1`, creating a named vector now avoids a migration later when you add `sparse` or `dense_v2`.

## Obsidian vault rules

- You may read any file under `docs/Musubi/`.
- Programmatic writes to vault-managed knowledge notes (curated plane) go through the `MusubiVault.write()` API, which handles debouncing, rename atomicity, and frontmatter schema validation — see [[06-ingestion/vault-sync]].
- Spec and ADR edits are a normal code-review change — commit them with the PR that motivated them, tagged `spec-update: <doc-path>` in the trailer.
- **Never** modify files in `docs/Musubi/_inbox/` outside the agent-created ticket pattern (cross-slice tickets, questions, lock files). That folder is operator-first.

## Escalation

If you're blocked, unsure, or notice a contradiction in the spec:

1. Don't guess. Don't "just make it work."
2. Create `docs/Musubi/_inbox/questions/<slice-id>-<short-title>.md` with: what you're trying to do, what you expected, what you observed, what options you see.
3. Mark your slice status as `blocked` in its frontmatter **and** via the `status:blocked` label on the GitHub Issue.
4. Move on to another slice you own (or release this one for another agent to pick up).

## Definition of Done for a slice

A slice is done when all are true:

- [ ] Every bullet in every relevant Test Contract is in one of the three Closure states above.
- [ ] Branch coverage ≥ 85 % on owned files (≥ 90 % on `src/musubi/planes/**` and `src/musubi/retrieve/**`).
- [ ] `make check` passes clean (format, lint, mypy, tests, coverage).
- [ ] `make agent-check` (aka `make vault-check`) is green.
- [ ] Docs in the corresponding `docs/Musubi/<section>/` are updated to reflect what was built (spec changes tagged `spec-update: <doc-path>` in the relevant commit).
- [ ] A human OR a `musubi-reviewer` sub-agent has reviewed and merged the PR; you did not self-approve (see [docs/AGENT-PROCESS.md §7](../../docs/AGENT-PROCESS.md#7-review)).
- [ ] The slice's entry in [[_slices/index]] is marked `done`; the GitHub Issue is closed via `Closes #<n>` in the PR.

## Prohibited patterns (automatic revert)

- Silent `time.sleep()` in production code paths (use async waits with timeouts).
- Environment-variable reads outside of `src/musubi/config.py`.
- Hardcoded hosts, ports, collection names, or thresholds.
- New top-level dependencies without an ADR.
- Mutating shared global state without a lock.
- `except Exception: pass`.
- `git push --force` on shared branches; `--no-verify` on commits.
- **Silently deferring a Test Contract bullet** — see the Closure Rule above.
- **Punting a method to a slice that doesn't own its code path** — see the Method-ownership rule above.
- Committing anything from `.agent-context.local.md`, `.env.local`, or files matching `.secrets/`.
