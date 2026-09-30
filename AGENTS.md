# AGENTS.md — the contract for every coding agent on this repo

This is the single entry point for every coding agent (Claude Code, Codex, Cursor, Gemini CLI, Grok,
Aider, Continue, Cline, Crush, anything else) and for human contributors. `CLAUDE.md`, `GEMINI.md`
and `.cursor/rules/musubi.mdc` are short pointers to this file, so there is exactly one copy of the
rules to keep true. Read it top to bottom **before any edit**.

## What Musubi is

Musubi (結び) is a three-plane shared-memory server for a small AI agent fleet: a standalone Python
service with a canonical HTTP API. Integrations (MCP, OpenClaw, Claude Code, Codex, Hermes, LiveKit, …)
are adapters that talk to that API; most live in their own `sourceblender/musubi-*` repos. The core
service, its SDK, deployment, contract tests and the architecture docs live here (see
[ADR 0015](docs/Musubi/13-decisions/0015-monorepo-supersedes-multi-repo.md) and
[ADR 0016](docs/Musubi/13-decisions/0016-vault-in-monorepo.md)).

## Repo map

```
src/musubi/                          implementation (Python 3.12, pydantic v2)
  types/ store/ planes/ retrieve/ lifecycle/ api/ sdk/ adapters/
tests/                               mirrors src/musubi/ path-for-path
docs/Musubi/                         architecture docs (an Obsidian vault): specs, ADRs
  00-index/                          conventions, guardrails, Definition of Done, glossary
  NN-<area>/                         specs per area, each with a Test Contract
  13-decisions/                      ADRs
  _tools/check.py                    docs health check (CI runs it)
deploy/                              Ansible, Docker, runbooks, smoke checks
.github/                             PR + issue templates, CI workflows
.agent-context.local.md              operator-only (gitignored): hosts, credentials pointers
```

## How work flows

Plain GitHub issues and pull requests. (Until 2026-09-30 the repo used "slices", per-task notes under
`docs/Musubi/_slices/`. That workflow is retired; its history is in git.)

1. **Start from an issue.** `gh issue list --state open`. If you were assigned one, use it. For a
   change with no issue, open one first unless it's a small docs or chore fix (see step 7).
2. **Say you're on it.** Assign yourself (`gh issue edit <n> --add-assignee @me`) and re-read the
   issue: if someone else is assigned, pick something else.
3. **Branch** off `main`: `git switch -c <type>/<short-name>` (e.g. `fix/blended-deadline`), and push
   with `-u`.
4. **Open a draft PR early** with `Closes #<n>` as the first line of the body, so work in progress is
   visible and nobody starts the same thing.
5. **Write the tests first,** from the Test Contract of the spec you're implementing (next section).
6. **Implement** the minimum to make them pass, within the rules below.
7. **Verify and hand off:** run the checks in "Before handoff", update the PR body so it describes
   what actually shipped, and mark it ready (`gh pr ready <m>`). A PR with no tracking issue says so on
   its first line: `No tracking Issue: <one-sentence reason>`.

## The non-negotiables

1. **Stay in scope.** Change what your issue and PR are about. If you need a change in another area
   (a shared type, another module's behaviour), say so in the PR or open an issue rather than folding
   it in quietly.
2. **The canonical API is frozen per version.** Changes to `src/musubi/api/`, `openapi.yaml` or
   `proto/` need an ADR if additive and a version bump if breaking.
3. **Tests first.** Every spec has a `## Test Contract` section. Your first commit is the test file
   realising it. A PR isn't mergeable until those tests pass and coverage is ≥ 85 % on the files you
   changed (≥ 90 % under `src/musubi/planes/**` and `src/musubi/retrieve/**`).
4. **Don't silently rewrite the spec.** If implementation forces a spec change, update the spec **in
   the same PR** with a `spec-update: <doc-path>` commit trailer.

## Test Contract Closure Rule

At handoff, every bullet in the spec's Test Contract is in exactly one of three states:

1. **Passing test** whose name transcribes the bullet (`test_create_sets_provisional_state` →
   `def test_create_sets_provisional_state(...)`), so the audit is a grep.
2. **Skipped with a reason:** `@pytest.mark.skip(reason="deferred to #<issue>: <why>")` (or `xfail`).
   The reason names the follow-up issue **and** justifies the deferral.
3. **Declared out of scope** in the PR description, naming the bullet, the reason and the follow-up
   issue.

**Silent omission is not a state.** A bullet with no matching test and no stated deferral is an
automatic request-changes. The PR template has a table for this.

## Ownership of a method

If a method's code lives in a module you're changing, you own it; don't defer it to "whoever exposes
it". Example: `EpisodicPlane.patch()` lives in `src/musubi/planes/episodic/`, so the planes change
implements it, even though the API exposes it as `PATCH /v1/episodic-memories/{id}`.

## Before handoff (the checks)

Run each and read its output:

1. **`make check`**: ruff format + ruff lint (whole repo, like CI) + mypy strict + pytest + coverage.
   Must exit 0.
2. **`make agent-check`**: docs health (frontmatter, spec Test Contracts, wikilinks). `✗` lines are
   errors and block; `⚠` lines are warnings. If it exits non-zero, look for `✗` first.
3. **`gh pr checks <pr>`**: remote CI. Local green and remote red means drift: stop and diagnose;
   never `--admin` past it.
4. **PR body:** first line `Closes #<n>.` (GitHub only links on `Closes`/`Fixes`/`Resolves`), or
   `No tracking Issue: <reason>`. The body describes what shipped, not the original plan.

Also:

- **Symmetric coverage.** If a docstring promises X and Y, both need tests. "Defensive branch" only
  excuses validation and error paths, never an advertised feature.
- **Deferred dependencies fail loud.** If you stub a real dependency behind an ADR, the production path
  must `raise NotImplementedError` or log at `ERROR`/`CRITICAL` saying it's stubbed. An `info` log is
  not a safety gate.

## Review and merge

- A **different** agent or a human reviews and merges. No self-approval.
- Merge through a PR only: no direct commits or force-pushes to `main`.
- Open the PR with the identity that did the work; GitHub attributes a squash merge to the PR's author.

## Hard prohibitions (automatic revert)

- Silent `time.sleep()` in production code (async waits with timeouts only).
- `os.environ` reads outside `src/musubi/config.py`.
- Hardcoded hosts, ports, collection names or thresholds (hostnames and IPs especially: see the
  placeholder scheme in `.agent-context.local.md`).
- New top-level dependencies without an ADR in `docs/Musubi/13-decisions/`.
- `except Exception: pass`.
- `git push --force` on shared branches; `--no-verify` on commits.
- Silently deferring a Test Contract bullet (see the Closure Rule).
- Committing anything in `.agent-context.local.md`, `.agent-brief.*.local.md`, `.env.local`,
  `.secrets/`, or files matching `*.pem` / `*.key` / `id_*`.

## Style (enforced by linters and CI)

- **Python 3.12,** strict mypy, ruff format + check. pydantic v2 models for every payload; dicts only
  at the Qdrant boundary.
- **Errors:** `Result[T, E]` at module boundaries with typed error dataclasses. Unhandled errors become
  5xx with correlation IDs at the API layer.
- **Async public surface;** internal sync is fine with no I/O.
- **Structured JSON logs,** one field per concept, never an f-string message; correlation IDs
  propagate. **No `print()`.**
- **Import discipline:** `sdk/*` imports `types/*` only; `adapters/*` imports `sdk` + `types` only;
  `api/*` composes `planes/*` + `retrieve/*` + `lifecycle/*` (enforced by review today).
- **Qdrant:** batch with `batch_update_points`; never loop `set_payload`.
- **Conventional Commits:** `feat(scope): …`, `fix(scope): …`, `test(scope): …`, `docs(scope): …`,
  `chore(scope): …`, `refactor(scope): …`.
- **Comments explain why, not what.** Full guide: [conventions](docs/Musubi/00-index/conventions.md).

## Commands

```bash
make install           # uv sync --extra dev
make check             # ruff format --check + ruff check + mypy --strict + pytest + coverage
make agent-check       # docs health (frontmatter, spec Test Contracts, wikilinks)
make test-integration  # integration tests against a docker stack
```

## Agent identification

In PR descriptions and commit trailers, identify the tool and model family, e.g. `codex-gpt5`,
`gemini-3-1`, `cursor-claude`, `claude-code-opus`, so reviewers can see which tool did which work.

## When you're stuck

Don't guess and don't "just make it work". Comment on the issue or draft PR with the goal, what you
expected, what you observed and the options you see, then ask. Don't hold an issue assignment while
you're blocked on someone else: unassign and say why.

## Definition of Done

- [ ] Every Test Contract bullet is in closure state 1, 2 or 3.
- [ ] Coverage ≥ 85 % on changed files (≥ 90 % under `planes/**` and `retrieve/**`).
- [ ] `make check` and `make agent-check` green; `gh pr checks` green.
- [ ] Spec updated in the same PR if behaviour changed (`spec-update:` trailer).
- [ ] PR body starts with `Closes #<n>.` (or `No tracking Issue: <reason>`) and describes what shipped.
- [ ] Reviewed and merged by someone other than the author.

Expanded rules: [docs/Musubi/00-index/agent-guardrails.md](docs/Musubi/00-index/agent-guardrails.md)
and [definition-of-done.md](docs/Musubi/00-index/definition-of-done.md). If anything contradicts this
file, ask before acting: contradictions are bugs.
