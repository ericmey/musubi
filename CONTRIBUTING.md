# Contributing to Musubi

First — thank you for even considering it. This is a personal project that I've opened up for others to follow, fork, and build on; outside contributions aren't required for the project to move forward, but they're genuinely welcome when they fit.

## The short version

1. **Open an issue first.** Bug, feature, or question — having a tracked conversation lets us agree on scope before code gets written.
2. **One focused change per PR.** Small, reviewable diffs that do what their issue says.
3. **Tests first, implementation second.** Every module has a Test Contract; your PR's first commit should be the test file.
4. **`make check` must pass.** Format, lint, type-check, and full test suite.
5. **Conventional commits.** The release automation reads them.

Everything else expands on these.

## Before you start

Please read [AGENTS.md](AGENTS.md), the single contract for human and AI contributors alike (CLAUDE.md, GEMINI.md and the Cursor rules just point to it). The non-negotiable rules:

1. **Stay in scope.** Change what your issue and PR are about; raise changes to other areas in the PR or a new issue instead of folding them in.
2. **The canonical API is frozen per version.** Additive changes require an ADR; breaking changes bump the major.
3. **Tests first.** Period.
4. **Don't silently rebase the spec.** If your implementation forces a spec change, update the spec file in the same PR and tag the commit with a `spec-update:` trailer.

Full text: [AGENTS.md](AGENTS.md).

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
make agent-check       # docs health: frontmatter, spec Test Contracts, wikilinks
```

## Specs and Test Contracts

The architecture docs under [`docs/Musubi/`](docs/Musubi/) hold a spec per area. Each spec that
describes behaviour has a **Test Contract**: named pytest functions to write first, which the code must
then make pass. If your change implements or alters a spec, its Test Contract drives your tests, and
any spec change goes in the same PR with a `spec-update:` trailer. If there's no spec for what you want
to build, say so in the issue; the spec can be drafted from `docs/Musubi/_templates/`.

## Workflow

```bash
# 1. Take the issue
gh issue edit <n> --add-assignee @me

# 2. Branch + draft PR immediately (visibility > speed)
git switch -c <type>/<short-name>
gh pr create --draft --base main \
  --title "<type>(<scope>): <subject>" \
  --body "Closes #<n>."   # exact keyword; auto-closes the issue on merge

# 3. First commit = the test file
# 4. Implement, commit, push
# 5. `make check` and `make agent-check` pass locally before marking the PR ready
# 6. Someone else reviews and merges; we don't self-approve
# 7. After merge, release-please picks it up for the next version bump
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

Not sure where something fits? Open an issue with the `question` label — it's the lowest-cost way to start a conversation. Once [Discussions](https://github.com/sourceblender/musubi/discussions) is enabled, longer design conversations move there.

## Code of Conduct

This project operates under a [Code of Conduct](CODE_OF_CONDUCT.md) based on the Contributor Covenant. By contributing you agree to abide by it.

---

Thank you again. Even opening an issue that turns into "nah, that doesn't fit" is a contribution — it sharpens the scope.
