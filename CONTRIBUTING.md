# Contributing to Musubi

First — thank you for even considering it. This is a personal project that I've opened up for others to follow, fork, and build on; outside contributions aren't required for the project to move forward, but they're genuinely welcome when they fit.

## The short version

1. **Open an issue first.** Bug, feature, or question — having a tracked conversation lets us agree on scope before code gets written.
2. **One slice per PR.** Small, reviewable diffs. See [Slices](#slices) below.
3. **Tests first, implementation second.** Every module has a Test Contract; your PR's first commit should be the test file.
4. **`make check` must pass.** Format, lint, type-check, and full test suite.
5. **Conventional commits.** The release automation reads them.

Everything else expands on these.

## Before you start

Please read the top of [CLAUDE.md](CLAUDE.md) (for AI agents) or this file's sibling [AGENTS.md](AGENTS.md) (same content; different filename so tools like Claude Code, Cursor, Aider, and Codex all find their canonical config). These capture the non-negotiable rules:

1. **Stay inside your slice.** The slice file in [`docs/Musubi/_slices/`](docs/Musubi/_slices/) declares which paths a slice may write; violating that turns a review into a merge-conflict.
2. **The canonical API is frozen per version.** Additive changes require an ADR; breaking changes bump the major.
3. **Tests first.** Period.
4. **Don't silently rebase the spec.** If your implementation forces a spec change, update the spec file in the same PR and tag the commit with a `spec-update:` trailer.

Full text: [`docs/Musubi/00-index/agent-guardrails.md`](docs/Musubi/00-index/agent-guardrails.md).

## Dev setup

```bash
# Prerequisites: Python 3.12 + uv (https://docs.astral.sh/uv/)

make install           # uv sync --extra dev
make fmt               # ruff format
make lint              # ruff check
make typecheck         # mypy --strict
make test              # pytest + coverage (unit)
make check             # all of the above — the gate for every PR

# Integration + vault hygiene (slower, optional locally):
make test-integration
make agent-check       # vault frontmatter + slice DAG + spec hygiene
```

## Slices

Musubi is built as a sequence of reviewable "slices" — small, independently-reviewable changes that realise one unit of the architecture. Each slice lives as a markdown file in [`docs/Musubi/_slices/`](docs/Musubi/_slices/) with:

- `owns_paths` — files this slice is allowed to write
- `forbidden_paths` — files it may not touch
- a **Test Contract** — pytest functions to be written first; code must make them pass

If your contribution maps to an existing slice spec, great — claim the GitHub Issue tracking it and go. If it doesn't map to an existing slice, open an issue describing the work and the spec will be drafted (or you can draft it yourself; see `_templates/` in the vault).

## Workflow

```bash
# 1. Claim the issue
gh issue edit <n> --add-assignee @me \
  --add-label "status:in-progress" --remove-label "status:ready"

# 2. Branch + draft PR immediately (visibility > speed)
git switch -c slice/<slice-id>
gh pr create --draft --base main \
  --title "<type>(<scope>): <subject>" \
  --body "Closes #<n>."   # exact keyword; auto-closes the issue on merge

# 3. First commit = the test file
# 4. Implement, commit, push
# 5. `make check` must pass locally before flipping the PR ready-for-review
# 6. Another agent / human reviews — we don't self-approve

# 7. After merge, the slice status flips to done; release-please picks it up
#    for the next version bump.
```

## Commit style

Conventional Commits. The `type(scope): subject` shape is parsed by release-please:

- `feat(planes): …` — new capability (minor bump)
- `fix(ci): …` — bug fix (patch bump)
- `docs(adr): …` — documentation
- `chore(deps): …` — build / tooling / non-user-visible
- `perf(retrieve): …`, `refactor(lifecycle): …`, `test(api): …`

First line ≤ 70 chars. Body explains *why* (not *what* — the diff already shows what).

Include a `spec-update: <path>` trailer when your change also edits a spec file in the vault. Include `Co-Authored-By:` trailers if an AI agent (or another human) materially helped — this isn't a hide-the-agent project.

## Pull request expectations

- **First line of the body** must be `Closes #<issue-number>.` (exact keyword; GitHub only auto-closes on `Closes` / `Fixes` / `Resolves`). For PRs without a tracking issue — chore / CI hotfix / docs — include `No tracking Issue: <one-sentence reason>` so the absence is deliberate.
- **Design note.** If your PR makes a non-obvious choice (which approach, which tradeoff), describe it. Reviewers shouldn't have to reconstruct the decision from the diff.
- **Test plan.** Bulleted list of what you verified. If anything is deferred (e.g. an integration test that needs the live image), call it out.
- **CI must be green** before flipping to ready-for-review. `gh pr checks <n>` locally mirrors the sidebar on github.com.

## Style

- Python: black-compatible via ruff. Strict mypy. Full type hints on every public function.
- Data: pydantic v2 models for every payload. Dicts only at the Qdrant boundary.
- Errors: `Result[T, E]` at module boundaries — typed error dataclasses, not raised exceptions.
- Async vs sync: public surface is async. Internal worker loops can be sync if they don't touch I/O.
- Logging: structured JSON, one field per concept. No f-strings in log messages. Correlation IDs propagate.
- **No `print()`.**
- **Comments explain *why*, not *what*.**

Full style guide: [`docs/Musubi/00-index/conventions.md`](docs/Musubi/00-index/conventions.md).

## Prohibited patterns (automatic revert)

- Silent `time.sleep()` in production code paths — use async waits with timeouts.
- Environment-variable reads outside of `src/musubi/config.py`.
- Hardcoded hosts, ports, collection names, or thresholds.
- `except Exception: pass`.
- `git push --force` on shared branches.
- `--no-verify` on commits.
- Committing anything gitignored (`.env.local`, vault secrets, `.agent-context.local.md`).

## Questions

Not sure where something fits? Open an issue with the `question` label — it's the lowest-cost way to start a conversation. Once [Discussions](https://github.com/ericmey/musubi/discussions) is enabled, longer design conversations move there.

## Code of Conduct

This project operates under a [Code of Conduct](CODE_OF_CONDUCT.md) based on the Contributor Covenant. By contributing you agree to abide by it.

---

Thank you again. Even opening an issue that turns into "nah, that doesn't fit" is a contribution — it sharpens the scope.
