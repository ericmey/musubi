# GEMINI.md — contract for Gemini CLI agents

Gemini CLI reads this file at session start. Read it top to bottom **before any edit**.

The full rules applicable to *every* coding agent on this repo live in [AGENTS.md](AGENTS.md) at the repo root. This file duplicates the rules most likely to trip Gemini specifically, plus the Gemini-relevant differences — but if you rely on only this file and skip AGENTS.md, you will miss things. **Read AGENTS.md too.**

## What you are working on

Musubi (結び) — a three-plane shared-memory server for a small AI agent fleet. Python 3.12, pydantic v2, strict mypy, Qdrant + TEI + Ollama for inference. See [README.md](README.md) and [docs/architecture/00-index/index.md](docs/architecture/00-index/index.md).

## Required reads in order

1. [AGENTS.md](AGENTS.md) — the full contract for non-Claude agents.
2. [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md) — multi-agent concurrency model.
3. [docs/architecture/00-index/agent-guardrails.md](docs/architecture/00-index/agent-guardrails.md) — authoritative rules expansion.
4. [docs/architecture/00-index/conventions.md](docs/architecture/00-index/conventions.md) — style guide.

If `.agent-context.local.md` exists at the repo root, read it for operator-specific hosts / credentials / placeholder map. It's gitignored and on-machine only.

## The rules Gemini is most likely to trip

### Test Contract Closure Rule

Every bullet in every spec's `## Test Contract` section must be one of:
- **Passing test** with a function name matching the bullet text verbatim (with `_` for spaces).
- **Skipped** with `@pytest.mark.skip(reason="deferred to slice-<id>: <why>")`.
- **Declared out-of-scope** in the slice's `## Work log` with a named follow-up home.

Silent omission ⇒ automatic request-changes. Long-context models sometimes want to summarise or reorganise test coverage "more tidily" than the spec bullets suggest — don't. The verbatim-bullet convention is how audits work.

### Method-ownership rule

If the method's implementation file lives in your slice's `owns_paths`, you own it. You don't defer it to a slice that merely exposes it (e.g., don't defer plane methods to `slice-api-v0`; the API slice routes, the plane owns).

### Dual-update rule (vault ↔ GitHub Issue)

State changes update **both** the slice file's frontmatter **and** the GitHub Issue's labels, in the **same PR**:

| Transition | Issue command | Frontmatter change |
|---|---|---|
| Claim | `gh issue edit <n> --add-assignee @me --add-label status:in-progress --remove-label status:ready` | `status: ready → in-progress`, `owner: <your-agent-id>` |
| Handoff | `gh issue edit <n> --add-label status:in-review --remove-label status:in-progress` | `status: in-progress → in-review` |
| Block | `gh issue edit <n> --add-label status:blocked` + comment | `status: <prev> → blocked`, work-log entry |
| Done | PR body `Closes #<n>` (auto-close on merge) | `status: in-review → done` |

`make issue-check` detects drift. Drift is a merge-blocker at review time.

- **Frontmatter fields must be explicit, top-level key-value pairs.** Including `status/open` or `type/cross-slice` purely within the `tags:` array is insufficient. A markdown note requires explicit, top-level definitions like `status: open` and `type: cross-slice`. Always duplicate the structure of a known-good sibling file when creating new notes.

### Other hard prohibitions

- Silent `time.sleep()` in production; no `except Exception: pass`; no `os.environ` outside `src/musubi/config.py`.
- No edits to `src/musubi/types/`, `src/musubi/api/`, `openapi.yaml`, `proto/` unless your slice is `slice-types` or `slice-api-v*` respectively.
- No `git push --force` on shared branches; no `--no-verify`.
- No commits of `.agent-context.local.md`, `.agent-brief.*.local.md`, `.env.local`, `.secrets/`, or any `*.pem` / `*.key` / `id_*`.

## The seven-step workflow

1. `gh issue list --label "slice,status:ready"` → pick one.
2. Claim via Dual-update rule §Claim (both Issue label AND frontmatter).
3. `git switch -c slice/<slice-id>`, push with `-u`.
4. `gh pr create --draft --base v2` with **first line of body = `Closes #<n>.`** (exact keyword; GitHub doesn't auto-link otherwise).
5. First commit: the test file (every Test Contract bullet = function with verbatim name). **Test-first implies atomic, cleanly separated commits** — Don't merge implementation, tests, and documentation updates into a single monolithic "handoff" commit. Follow the sequence: `chore(take)`, `test(initial)`, `feat(impl)`, and finally `docs(handoff)`.
6. Implement. Respect `forbidden_paths`.
7. Before `gh pr ready`, run the **five handoff checks**:
   - `make check` (whole repo; matches CI)
   - `make tc-coverage SLICE=<id>` — exit 0
   - `make agent-check` — distinguish `✗` errors from `⚠` warnings; only `✗` blocks
   - `gh pr checks <pr>` — remote CI green too (not just local)
   - PR body first line is `Closes #<n>.` for slice PRs, or `No tracking Issue: <reason>` for chore/infra PRs

### Additional handoff rules Gemini is most likely to trip

- **Symmetric coverage.** A docstring promising X and Y needs tests for both. Defensive-branch coverage gaps are only OK for validation + error paths, not for advertised features.
- **Coverage thresholds are evaluated per-module, not just per-repo.** The global 85% fail-under in pytest does not guarantee compliance with the 90% floor specifically required for `src/musubi/planes/**`. Validate localized coverage individually using `uv run coverage report --include='src/musubi/<your-module>/*'` prior to handoff.
- **ADR-punted deps fail loud, not silently no-op.** If you stub a dependency, `raise NotImplementedError` or log at `ERROR`/`CRITICAL` — never just `info`.
- **PR body and code stay in sync.** If the design evolved during implementation, rewrite the description before handoff.
- **Ruff format is a strict gatekeeper, not just an auto-fixer.** `make check` executes `ruff format --check`. If any file merely *would* be reformatted, the script fails (exit 1). Prevent this by always running `uv run ruff format .` explicitly before committing.
- **Agent checks differentiate hard errors from soft warnings.** When `make agent-check` fails with a non-zero exit code, do not assume it's just noisy warnings. Always verify the output directly with `grep "^  ✗"` to locate the blocking hard errors.
- **"All handoff checks pass" is an empirical statement, not an assumption.** Do not claim all gates are green just because they passed prior to your last small edit. Re-verify each check individually (`make check`, `make tc-coverage`, `make agent-check`, `gh pr checks`) and actually read the output to confirm the fix didn't inadvertently introduce a regression.
- **If implementing reveals a bug in a sibling slice's code, the fix goes through _inbox/cross-slice/, not into your PR.** Your PR stays inside its owns_paths; the sibling fix lands as a separate follow-up (either the sibling's owner re-opens the slice briefly, or the operator lands a chore PR). Tempting to fix-in-place "while you're there" — don't. The slice-boundary is what lets parallel agents work without stepping on each other; any exception undermines the model.


## Style

- Python 3.12, strict mypy, ruff format + check, pydantic v2 on every payload.
- `Result[T, E]` at module boundaries (typed error dataclasses, not raised exceptions).
- Async public surface; internal sync OK if no I/O.
- Structured JSON logs; no f-strings in log messages; correlation IDs propagate.
- **No `print()`**.
- Conventional Commits. Same-PR spec changes get `spec-update: <doc-path>` trailer.

## Agent identification

When you append to a slice's `## Work log`, identify as `gemini-<version>` (e.g. `gemini-3-1`, `gemini-3-1-pro`). Keeps the history auditable.

## When Gemini's strengths fit best

Gemini's long context is useful for whole-vault reasoning — spec revisions and ADR drafting where cross-slice impact analysis matters. Prefer Gemini for:

- Revising specs after implementation discovered a mismatch (use `musubi-spec-author`-style restraint: declarative present-tense prose, no hedging).
- ADR drafting + cross-linking.
- Reviewing PRs that touch multiple slices.

Prefer Claude Code or Codex for single-slice implementation work; use the appropriate subagent/skill definitions in `.claude/` or `.agents/` for that workflow.

## When you're stuck

1. Drop a file at `docs/architecture/_inbox/questions/<slice-id>-<slug>.md`: goal, expectation, observation, options.
2. Flip slice + Issue to `blocked` (Dual-update rule).
3. Comment the Issue with the link to your question.
4. Pick another slice.

## Don't edit these from Gemini sessions

- `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.cursor/rules/*` — agent configuration. Changes affect every agent; operator-only.
- `.claude/agents/*`, `.claude/skills/*`, `.agents/skills/*` — agent/skill definitions. Operator-only unless your slice is explicitly about agent tooling.
- `docs/architecture/00-index/agent-guardrails.md`, `agent-handoff.md`, `definition-of-done.md`, `conventions.md` — meta-rules. Changes require operator authorization.

Everything else: stay in `owns_paths`, test-first, dual-update the state, make check green, hand off.
