---
title: Agent Entry Point (CLAUDE.md)
type: vault-readme
status: living-document
tags: [type/vault-readme, status/living-document, agents]
updated: 2026-04-17
reviewed: true
---

# Musubi — Coding Agent Entry Point

If you are a coding agent picking up work on Musubi, **read this file top to bottom before anything else.** It is the shortest path from zero context to your first productive commit.

## What Musubi is

Musubi (結び) is a three-plane shared memory server for a small AI agent fleet. It is a standalone Python service with a canonical HTTP/gRPC API. Every downstream interface (MCP, LiveKit, OpenClaw) is a separate repo that depends on the Musubi SDK. See [[README]] for the high-level pitch and [[00-index/index]] for the vault index.

## The non-negotiables (4 rules)

1. **Stay inside your slice.** Every planned unit of work has an explicit slice note in `_slices/<slice-id>.md` with `owns_paths` and `forbidden_paths`. You may read anywhere; you may write only to `owns_paths`.
2. **The canonical API is frozen per version.** If your slice is not `slice-api-*`, you do not modify `musubi/api/`, `openapi.yaml`, or `proto/`. Additive changes require an ADR; breaking changes bump the version.
3. **Tests first.** Every spec has a **Test Contract** section. Your first commit in a slice is the test file realising it. The PR is not mergeable until those tests pass and branch coverage ≥ 85% on owned files.
4. **Do not silently rebase the spec.** If your implementation forces a spec change, update the spec file in the same PR and tag the commit `spec-update: <doc-path>`.

Full text: [[00-index/agent-guardrails]].

## Your first 30 minutes

1. **Pick a slice.** Open [[_slices/index]] or the [[_slices/slice-dag.canvas|slice DAG]]. Choose a slice with `status: ready` whose every `depends-on` slice is `status: done`. No such slice? Pick up a cross-slice ticket in `_inbox/cross-slice/` instead.
2. **Lock it.** Create `_inbox/locks/<slice-id>.lock` (see [[00-index/agent-handoff#2. Lock]]). Flip the slice's `status` frontmatter to `in-progress` and set `owner:` to your agent id.
3. **Read the specs.** Every slice note links its source specs. Read them. Also open the section's `CLAUDE.md` (e.g. `04-data-model/CLAUDE.md`) for local rules.
4. **Write the test file.** Translate the spec's **Test Contract** section into pytest functions, one per bullet. Commit as `test(<scope>): initial test contract for <slice-id>`. Tests fail — that's expected.
5. **Implement.** Write the minimum code to make tests pass. Respect `forbidden_paths`. If you need a cross-slice change, open `_inbox/cross-slice/<slice-id>-<target>.md` and flip your slice to `blocked`.
6. **Verify.** `make check` must pass (ruff format + ruff lint + mypy --strict + pytest + coverage).
7. **Open PR.** Flip slice `status` to `in-review`. Append the PR URL to the slice note's **PR links** section. Remove the lock file.

See [[00-index/agent-handoff]] for the complete lifecycle.

## Commands you will run

```bash
make fmt            # ruff format
make lint           # ruff check
make typecheck      # mypy --strict
make test           # pytest + coverage (unit)
make test-integration  # integration (docker qdrant)
make check          # all of the above

# Vault-state gates (see _tools/README.md):
make agent-check    # vault frontmatter + slice DAG + spec hygiene
make slice-check    # slices only — DAG, locks, owns_paths conflicts
make spec-check     # specs only — Test Contracts, implements:, tags
```

## Paths you will touch

- `musubi/` — core server code. Subdirs are owned by specific slices (see `_slices/`).
- `tests/` — unit tests mirror source paths exactly. `musubi/retrieve/scoring.py` → `tests/retrieve/test_scoring.py`.
- `docs/architecture/` — this vault. You may edit the spec your slice implements (same PR, tagged `spec-update:`).
- `_slices/<your-slice-id>.md` — your work log and status.
- `_inbox/locks/<your-slice-id>.lock` — presence signal.

## Paths you may NOT touch without authorization

- `musubi/api/`, `openapi.yaml`, `proto/` — canonical API surface. Frozen per version.
- `musubi/types/`, `musubi/schema/`, `musubi/models.py` — core types. Only `slice-types` writes here.
- Any file owned by another active slice (check `_slices/` + `_inbox/locks/`).
- `docs/architecture/00-index/conventions.md`, `agent-guardrails.md`, `agent-handoff.md`, `definition-of-done.md` — meta-rules. Changes require a human.

## Style (enforced by linters)

- **Python:** black-compatible via ruff. Strict mypy. Full type hints on every public function.
- **Data:** pydantic v2 models for every payload. Dicts only at the Qdrant boundary.
- **Errors:** `Result[T, E]` at module boundaries — typed error dataclasses, not raised exceptions. Unhandled errors are caught at the API layer and become 5xx with correlation IDs.
- **Async vs sync:** public surface is async. Internal worker loops can be sync if they don't touch I/O.
- **Logging:** structured JSON, one field per concept. No f-strings in log messages. Correlation IDs propagate.
- **No `print()`.**
- **Comments explain *why*, not *what*.**

See [[00-index/conventions]] for the full style guide.

## When you get stuck

1. Don't guess. Don't "just make it work."
2. Drop a file in `_inbox/questions/<slice-id>-<slug>.md` with: what you're trying to do, what you expected, what you observed, what options you see.
3. Flip your slice to `blocked`.
4. Pick up another slice.

## When you ship

1. PR merged, CI green.
2. Slice `status: done`.
3. Lock removed.
4. Downstream slices (`blocks:`) are now eligible — notify in the slice's work log so the next agent sees it.

## Cheat sheet

| Need to… | Look at |
|---|---|
| See the whole architecture visually | [[00-index/architecture.canvas]] |
| Pick a slice | [[_slices/slice-dag.canvas]] or [[_slices/index]] |
| Know what "done" means | [[00-index/definition-of-done]] |
| Coordinate with another agent | [[00-index/agent-handoff]] |
| Find a test fixture | [[_slices/test-fixtures]] |
| Understand a term | [[00-index/glossary]] |
| See existing decisions | [[13-decisions/index]] |
| Understand the POC you're migrating from | [[02-current-state/index]] |

## Prohibited patterns (automatic revert)

- Silent `time.sleep()` in production code paths (use async waits with timeouts).
- Environment-variable reads outside of `musubi/config.py`.
- Hardcoded hosts, ports, collection names, or thresholds.
- New top-level dependencies without an ADR.
- Mutating shared global state without a lock.
- `except Exception: pass`.
- `git push --force` on shared branches.
- `--no-verify` on commits.

## A minimal first-PR checklist

- [ ] Slice locked in `_inbox/locks/`.
- [ ] Slice frontmatter `status: in-progress`, `owner:` set.
- [ ] Test file landed in the first commit.
- [ ] `make check` passes.
- [ ] PR description references the slice id and the specs implemented.
- [ ] Definition of Done items checked.

Now go read [[_slices/index]] and pick one.
