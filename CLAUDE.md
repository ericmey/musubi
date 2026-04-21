# Musubi — Coding Agent Entry Point

If you are a coding agent (Claude Code, Claude Cowork, Codex, Cursor, Gemini CLI, Grok, Aider, Continue, Crush, Cline, or anything else) picking up work on Musubi, **read this file top to bottom before anything else.** It is the shortest path from zero context to your first productive commit.

This file is the canonical working agreement. Other agent tools read their own shim files ([AGENTS.md](AGENTS.md), [GEMINI.md](GEMINI.md), [.cursor/rules/musubi.mdc](.cursor/rules/musubi.mdc)) that point back here to avoid drift.

## What Musubi is

Musubi (結び) is a three-plane shared memory server for a small AI agent fleet. It is a standalone Python service with a canonical HTTP/gRPC API. Every downstream interface (MCP, LiveKit, OpenClaw) is an adapter that depends on the Musubi SDK. All of those components — core, SDK, adapters, the architecture vault, deployment, and CI — live in this single repo (see [ADR 0015](docs/Musubi/13-decisions/0015-monorepo-supersedes-multi-repo.md) and [ADR 0016](docs/Musubi/13-decisions/0016-vault-in-monorepo.md)).

## The repo at a glance

```
~/Projects/musubi/                   ← this repo (github.com/ericmey/musubi)
├── src/musubi/                      ← implementation (Python 3.12, pydantic v2)
│   ├── types/                       ← shared types (slice-types)
│   ├── store/                       ← Qdrant layout + bootstrap (slice-qdrant-layout)
│   ├── planes/ retrieve/ lifecycle/ api/ sdk/ adapters/   ← future slices
├── tests/                           ← mirrors src/musubi/ path-for-path
├── docs/
│   ├── AGENT-PROCESS.md             ← multi-agent concurrency model — read this
│   └── architecture/                ← the Obsidian vault — the specs
│       ├── 00-index/                ← conventions, guardrails, handoff, DoD
│       ├── _slices/                 ← one coding task per file
│       ├── 04-data-model/ …         ← specs per area
│       └── 13-decisions/            ← ADRs
├── .claude/
│   ├── agents/                      ← musubi-slice-worker, reviewer, spec-author
│   └── skills/                      ← pick-slice, handoff, spec-check
├── .cursor/rules/                   ← Cursor rules (shims pointing here)
├── .github/                         ← PR + Issue templates, CI workflows
├── AGENTS.md GEMINI.md              ← shims for other coding-agent tools
└── .agent-context.local.md          ← gitignored, operator-only hosts/creds pointers
```

## The non-negotiables (4 rules)

1. **Stay inside your slice.** Every planned unit of work has an explicit slice note at `docs/Musubi/_slices/<slice-id>.md` with `owns_paths` and `forbidden_paths`. You may read anywhere; you may write only to `owns_paths`.
2. **The canonical API is frozen per version.** If your slice is not `slice-api-*`, you do not modify `src/musubi/api/`, `openapi.yaml`, or `proto/`. Additive changes require an ADR; breaking changes bump the version.
3. **Tests first.** Every spec has a **Test Contract** section. Your first commit in a slice is the test file realising it. The PR is not mergeable until those tests pass and branch coverage ≥ 85 % on owned files (90 % for `planes/**` / `retrieve/**`).
4. **Do not silently rebase the spec.** If your implementation forces a spec change, update the spec file in the same PR and tag the commit `spec-update: <doc-path>`.

Full text: [docs/Musubi/00-index/agent-guardrails.md](docs/Musubi/00-index/agent-guardrails.md).

## Your first 30 minutes

1. **Read the big three** (in order): this file, [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md), [docs/Musubi/00-index/agent-guardrails.md](docs/Musubi/00-index/agent-guardrails.md).
2. **Check for local operator context.** If `.agent-context.local.md` exists at the repo root, read it for operator-specific hosts / credentials pointers. If it does not, ask the operator to create one from the template — do not proceed with anything that touches infrastructure (SSH, Ansible, model pulls) without it.
3. **Pick up work.** Either:
   - **You were assigned a GitHub Issue** → use that slice. Run the `pick-slice` skill (Claude Code users) or follow the manual steps in [docs/AGENT-PROCESS.md §5](docs/AGENT-PROCESS.md#5-how-to-claim-a-slice-step-by-step).
   - **You're self-selecting** → `gh issue list --label "slice,status:ready" --state open` and pick one whose `depends-on` slices are all `status: done` (or have first cuts merged).
4. **Lock it.** Atomic GitHub Issue claim (`gh issue edit <n> --add-assignee @me`) is the authoritative lock. File-based `_inbox/locks/<slice-id>.lock` is a secondary signal.
5. **Branch + draft PR** immediately (`slice/<slice-id>`, then `gh pr create --draft`). Visible work-in-progress prevents duplicate starts.
6. **Flip slice frontmatter** to `in-progress` and set `owner:` to your agent id.
7. **Write the test file** from the spec's Test Contract. Commit as `test(<scope>): initial test contract for <slice-id>`.
8. **Implement** the minimum to make tests pass. Respect `forbidden_paths`.
9. **Verify.** `make check` must pass (ruff format + ruff lint + mypy --strict + pytest + coverage).
10. **Hand off.** Flip slice `status: in-review`, mark PR ready, append the work-log entry, remove the lock file.

See [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md) for the full multi-agent lifecycle.

## Commands you will run

```bash
make install           # uv sync --extra dev
make fmt               # ruff format
make lint              # ruff check
make typecheck         # mypy --strict
make test              # pytest + coverage (unit)
make check             # all of the above

# Once these slices land:
make test-integration  # integration (docker qdrant)
make agent-check       # vault frontmatter + slice DAG + spec hygiene (future; today the vault-check GitHub Action covers this on PR)
```

## Paths you will touch

- `src/musubi/<module>/` — your slice's owned code.
- `tests/<module>/` — tests mirror source paths 1-for-1. `src/musubi/retrieve/scoring.py` → `tests/retrieve/test_scoring.py`.
- `docs/Musubi/<NN-section>/<topic>.md` — the spec your slice implements. Edit it (same PR, `spec-update:` trailer) only if your implementation forced a change.
- `docs/Musubi/_slices/<your-slice-id>.md` — your work log and status.
- `docs/Musubi/_inbox/locks/<your-slice-id>.lock` — secondary presence signal.

## Paths you may NOT touch without authorization

- `src/musubi/api/`, `openapi.yaml`, `proto/` — canonical API surface. Frozen per version.
- `src/musubi/types/` — shared types. Only `slice-types` writes here.
- Any file owned by another active slice (check `docs/Musubi/_slices/` + GitHub Issues with `status:in-progress`).
- `docs/Musubi/00-index/conventions.md`, `agent-guardrails.md`, `agent-handoff.md`, `definition-of-done.md` — meta-rules. Changes require a human.
- `.claude/`, `AGENTS.md`, `GEMINI.md`, `.cursor/rules/` — agent configuration. Changes affect every agent and need explicit operator sign-off.

## Style (enforced by linters)

- **Python:** black-compatible via ruff. Strict mypy. Full type hints on every public function.
- **Data:** pydantic v2 models for every payload. Dicts only at the Qdrant boundary.
- **Errors:** `Result[T, E]` at module boundaries — typed error dataclasses, not raised exceptions. Unhandled errors are caught at the API layer and become 5xx with correlation IDs.
- **Async vs sync:** public surface is async. Internal worker loops can be sync if they don't touch I/O.
- **Logging:** structured JSON, one field per concept. No f-strings in log messages. Correlation IDs propagate.
- **No `print()`**.
- **Import discipline:** `sdk/*` imports `types/*` only. `adapters/*` imports `sdk+types` only. `api/*` is the only module allowed to compose `planes/*` + `retrieve/*` + `lifecycle/*`. Enforced by lint in a future pass; enforced by review until then.
- **Comments explain *why*, not *what*.**

See [docs/Musubi/00-index/conventions.md](docs/Musubi/00-index/conventions.md) for the full style guide.

## Multi-agent coordination

Multiple coding agents work on this repo concurrently. The rules are in [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md); the short version:

- **GitHub Issues are the lock board** (atomic assignment, visible in GH UI). File locks are secondary.
- **One slice per branch**, branch named `slice/<slice-id>`.
- **Draft PR from the moment you start** so other agents see work-in-progress.
- **You don't self-approve.** A different agent (or human) reviews before merge.
- **Force-push to `main` is forbidden.** Direct commits to `main` are forbidden. Merge via PR only.

## Agent selection (routing)

Not all agents are equivalent. Rough guide (details in [docs/AGENT-PROCESS.md §4](docs/AGENT-PROCESS.md#4-who-should-do-what-agent-selection)):

- **Claude Code (Opus)** — default for slice implementation.
- **Claude Cowork** — long autonomous work spanning multiple slices.
- **Codex** — small slices, CI tweaks, throw-away prototypes.
- **Cursor** — interactive debugging + reading across the tree.
- **Gemini** — long-context spec revision (the whole vault fits).
- **Grok** — second-opinion reviews / diversity on contested calls.

## When you get stuck

1. Don't guess. Don't "just make it work."
2. Drop a file at `docs/Musubi/_inbox/questions/<slice-id>-<slug>.md` with: what you're trying to do, what you expected, what you observed, what options you see.
3. Flip your slice to `blocked` (frontmatter + Issue label).
4. Pick up another slice.

## Before handoff (the five checks)

Before you flip a slice `in-progress → in-review` and mark a PR ready-for-review, run and carefully read the output of each:

1. **`make check`** — ruff format + ruff lint (both scan the whole repo, matching CI) + mypy strict + pytest + coverage. Must exit 0.
2. **`make tc-coverage SLICE=<slice-id>`** — Closure Rule audit. Every Test Contract bullet must be in one of the three Closure states (passing / `@pytest.mark.skip(reason="deferred to slice-X: ...")` / declared out-of-scope in the slice's `## Work log`). Must exit 0.
3. **`make agent-check`** — vault-hygiene audit. **Distinguish `✗` errors from `⚠` warnings**: errors block, warnings don't. If exit code is non-zero, grep the output for `✗` first — don't assume a pre-existing warning is the cause.
4. **`gh pr checks <your-pr-number>`** — remote GitHub Actions state. Remote CI can fail on drift that local gates don't surface. If local green + remote red, stop and diagnose — don't `--admin` past it.
5. **PR body linkage:**
   - **Slice PRs:** first line of the body must be `Closes #<issue-number>.` on its own. GitHub only auto-links on those exact keywords (`Closes`, `Fixes`, `Resolves`, case-insensitive — prefer `Closes` for consistency). Missing the keyword breaks auto-close on merge, the "Linked issues" sidebar, and the Dual-update drift check's PR↔Issue path.
   - **Chore / infra / docs PRs with no tracking Issue:** include a line `No tracking Issue: <one-sentence reason>` so the absence is a deliberate choice, not an oversight.

Additional handoff-readiness rules:

- **Symmetric coverage.** If a class, module, or function docstring promises features X *and* Y, both need tests. "Defensive branch" is only a valid coverage-gap justification for validation / exception paths — never for a feature promised in the docstring.
- **ADR-punted dependencies must fail loud, not silently no-op.** If you defer wiring a real dependency (e.g. production scheduler, LLM client) behind an ADR, the production path must `raise NotImplementedError` or emit at `ERROR`/`CRITICAL` with an explicit "THIS DOES NOT TICK / IS STUBBED" message. An `info` log is not a safety gate.
- **Keep PR body and code in sync.** If the design evolved during implementation, rewrite the design note in the PR description before marking ready-for-review. Reviewers shouldn't have to reconcile stale intent against actual behaviour.

## When you ship

1. PR merged, CI green.
2. Slice `status: done`, `owner:` retained (so we know who shipped it).
3. Issue auto-closes via `Closes #<n>` in the PR body.
4. Downstream slices (`blocks:`) become eligible — comment on them or the closed issue so the next agent in line sees it.

## Prohibited patterns (automatic revert)

- Silent `time.sleep()` in production code paths (use async waits with timeouts).
- Environment-variable reads outside of `src/musubi/config.py`.
- Hardcoded hosts, ports, collection names, or thresholds.
- New top-level dependencies without an ADR.
- Mutating shared global state without a lock.
- `except Exception: pass`.
- `git push --force` on shared branches.
- `--no-verify` on commits.
- Committing anything from `.agent-context.local.md`, `.env.local`, or files matching `.secrets/` — the `.gitignore` blocks them but don't try to work around it.

## Cheat sheet

| Need to… | Look at |
|---|---|
| Understand the multi-agent flow | [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md) |
| See the whole architecture visually | [docs/Musubi/00-index/architecture.canvas](docs/Musubi/00-index/architecture.canvas) |
| Pick a slice | [docs/Musubi/_slices/slice-dag.canvas](docs/Musubi/_slices/slice-dag.canvas) or [docs/Musubi/_slices/](docs/Musubi/_slices/) |
| Know what "done" means | [docs/Musubi/00-index/definition-of-done.md](docs/Musubi/00-index/definition-of-done.md) |
| Coordinate with another agent | [docs/Musubi/00-index/agent-handoff.md](docs/Musubi/00-index/agent-handoff.md) |
| Find a test fixture | [docs/Musubi/_slices/test-fixtures.md](docs/Musubi/_slices/test-fixtures.md) |
| Understand a term | [docs/Musubi/00-index/glossary.md](docs/Musubi/00-index/glossary.md) |
| See existing decisions | [docs/Musubi/13-decisions/](docs/Musubi/13-decisions/) |
| Operator-only hosts / credentials | `.agent-context.local.md` at repo root (not in git) |

## A minimal first-PR checklist

- [ ] GitHub Issue claimed (you are the assignee; label is `status:in-progress`).
- [ ] `_inbox/locks/<slice-id>.lock` dropped in the vault as a secondary signal.
- [ ] Slice frontmatter: `status: in-progress`, `owner:` set.
- [ ] Draft PR opened; **body first line is `Closes #<issue>.`** (or `No tracking Issue: <reason>` for chore/infra).
- [ ] First commit on the branch is the test file.
- [ ] `make check` + `make tc-coverage SLICE=<id>` + `make agent-check` all exit 0.
- [ ] `gh pr checks <pr>` reports green *before* flipping to ready-for-review.
- [ ] Definition of Done items checked.

Now go read [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md) and either open the Issue you were assigned or `gh issue list --label "slice,status:ready"`.
