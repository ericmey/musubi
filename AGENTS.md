# AGENTS.md — shared entry point for all coding agents

This file is the common landing page for AI coding agents (Codex, Cursor, Continue, Crush, Aider, Cline, and similar tools that look for `AGENTS.md` at the repo root).

**For the full agent guide, read `CLAUDE.md` at the repo root.** It contains the complete working agreement: non-negotiables, slice discipline, import rules, commit conventions, and the path map.

## The short version

- **Architecture specs live at `docs/architecture/`** — the Obsidian vault is part of this repo.
- **Work is sliced.** Every coding task maps to one file under `docs/architecture/_slices/slice-<id>.md`. That file names your `owns_paths`, `forbidden_paths`, and a Test Contract you implement first.
- **Tests first.** The first commit on your slice branch is the test file. Implementation follows.
- **Stay in your slice.** Read anywhere. Write only inside `owns_paths`.
- **The canonical API is frozen per version.** Don't touch `src/musubi/api/`, `openapi.yaml`, or `proto/` unless your slice is `slice-api-*`.
- **Coordinate via GitHub Issues.** One issue per active slice; assignee = owner. Don't start work without claiming an Issue.
- **`make check` must pass** before opening a PR for review.

## Conventions that don't vary by agent

- **Python 3.12, strict mypy, ruff format + lint, pydantic v2** — no exceptions.
- **Conventional Commits** — `feat(scope): ...`, `fix(scope): ...`, `docs(scope): ...`, `test(scope): ...`, `chore(scope): ...`. Trailer `spec-update: <doc-path>` when the spec changed in the same PR.
- **No `--no-verify` commits, no `git push --force` on shared branches, no `except Exception: pass`, no silent `time.sleep()` in production code.**

## How to start a session

1. Read `CLAUDE.md` at the repo root.
2. Read `docs/architecture/00-index/agent-guardrails.md`.
3. Read `docs/AGENT-PROCESS.md` — the multi-agent concurrency model.
4. Either continue an assigned slice (check GitHub Issues with your assignee) or pick a `ready` slice via the `pick-slice` skill / checklist in `docs/AGENT-PROCESS.md`.

## Why this file exists alongside CLAUDE.md

Claude Code reads `CLAUDE.md`. Codex reads `AGENTS.md`. Gemini CLI reads `GEMINI.md`. Cursor reads `.cursor/rules/*.mdc`. All of these exist at the root of this repo and point at the same underlying guide so content doesn't drift. If you're writing or revising rules, edit `CLAUDE.md` — the others are short shims that reference it.
