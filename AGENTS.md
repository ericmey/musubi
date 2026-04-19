# AGENTS.md — contract for every coding agent on this repo

This is the shared entry point for every non-Claude agent tool (Codex, Cursor, Continue, Aider, Cline, Crush, and anything else that reads `AGENTS.md` at the repo root). Claude Code agents also read [CLAUDE.md](CLAUDE.md); the two files express the same contract, so don't diff them — if you find a contradiction, CLAUDE.md is the source for Claude-specific guidance and this file is the source for everyone else, but the **rules below apply to every agent on the project regardless of tool.**

Read this file top to bottom **before any edit**. The rules are not suggestions. Every coding agent on this repo — human-prompted or autonomous — operates under this contract.

## What Musubi is

Musubi (結び) is a three-plane shared-memory server for a small AI agent fleet. Standalone Python service, canonical HTTP/gRPC API, adapters (MCP, LiveKit, OpenClaw) that depend on the SDK. All code, SDK, adapters, deployment, contract tests, and the architecture vault live in this one repo (see [ADR 0015](docs/architecture/13-decisions/0015-monorepo-supersedes-multi-repo.md) and [ADR 0016](docs/architecture/13-decisions/0016-vault-in-monorepo.md)).

## Repo map

```
src/musubi/                          implementation (Python 3.12, pydantic v2)
  types/ store/ planes/ retrieve/ lifecycle/ api/ sdk/ adapters/
tests/                               mirrors src/musubi/ path-for-path
docs/
  AGENT-PROCESS.md                   multi-agent concurrency — required read
  architecture/                      the Obsidian vault (specs, ADRs, slices)
    _slices/slice-<id>.md            one coding task per file
    _inbox/locks/ cross-slice/ questions/ research/
    00-index/agent-guardrails.md     authoritative expansion of this file
    13-decisions/                    ADRs
    _tools/                          vault health checks (check.py, …)
.claude/ .agents/ .cursor/ GEMINI.md  per-tool agent configuration
.github/                             PR + Issue templates, CI workflows
.agent-context.local.md              operator-only (gitignored): hosts, creds pointers
```

## The non-negotiables

1. **Stay inside your slice.** Your slice file at `docs/architecture/_slices/<slice-id>.md` names `owns_paths` + `forbidden_paths`. Read anywhere; write only to `owns_paths`. Cross-slice work opens a ticket at `docs/architecture/_inbox/cross-slice/<slice>-<target>.md` + a `cross-slice` GitHub Issue, and your slice flips to `status: blocked`.
2. **The canonical API is frozen per version.** Only `slice-api-v*` agents modify `src/musubi/api/`, `openapi.yaml`, `proto/`. Additive changes require an ADR; breaking changes bump the version.
3. **Tests first.** Every spec's `## Test Contract` section is a list of bullets. Your *first commit* on the branch is the test file realising those bullets. Implementation commits follow. PR isn't mergeable until tests pass + coverage ≥ 85 % on owned files (≥ 90 % on `src/musubi/planes/**` and `src/musubi/retrieve/**`).
4. **Do not silently rebase the spec.** If implementation forces a spec change, update the spec file **in the same PR** with a `spec-update: <doc-path>` trailer on the commit.

## Test Contract Closure Rule

At handoff, **every bullet in the spec's Test Contract is in exactly one of three states:**

1. **Passing test** whose name transcribes the bullet text verbatim (with `_` for spaces). Example: a spec bullet `test_create_sets_provisional_state` appears in the test file as `def test_create_sets_provisional_state(...)` and passes. Spec bullet = test-function-name convention makes the audit mechanical (grep).
2. **Skipped test with reason.** `@pytest.mark.skip(reason="deferred to slice-<id>: <one-line-why>")` or `@pytest.mark.xfail(reason="...")`. The reason must name the follow-up slice **and** justify the deferral.
3. **Declared out-of-scope in the slice's work log.** An entry under `## Work log` in `_slices/<slice-id>.md` naming the bullet, reason, and follow-up home. A GitHub Issue for the follow-up must exist.

**Silent omission is not one of the three states.** A spec Test Contract bullet with no matching test and no work-log justification is an **automatic request-changes on review.** The `musubi-reviewer` sub-agent (or a human reviewer) surfaces silent omissions as a Must-fix.

This rule exists because on 2026-04-18 a slice first-cut silently deferred `patch()` + `delete()` + `access_count` to downstream slices — some correctly scoped, some not. Other agents reading the spec could not tell which bullets were consciously punted vs. overlooked. This rule closes that loophole.

## Method-ownership rule

**If the method's code would live inside your `owns_paths`, you own the method.**

You may NOT defer a method to a slice that merely *exposes* it through a different surface. Example: `EpisodicPlane.patch()` is owned by `slice-plane-episodic` (its code lives in `src/musubi/planes/episodic/`). The API slice (`slice-api-v0`) exposes it via `PATCH /v1/episodic-memories/{id}` but **does not own the implementation.** Pushing `patch()` onto `slice-api-v0` mis-scopes the work.

Mechanical test when in doubt: "Would the other slice's `owns_paths` list contain the file the method lives in?" Yes → defer. No → you own it.

## Dual-update rule (vault frontmatter ↔ GitHub Issue)

**Every slice-state change updates both the vault file AND the Issue, in the same PR.**

GitHub Issues are the authoritative lock (atomic assignment across agent machines — see [docs/AGENT-PROCESS.md](docs/AGENT-PROCESS.md)). The slice file's frontmatter is the authoritative intent record (audited, reviewed in PRs, read in Obsidian). They must not be allowed to drift.

### Claim (ready → in-progress)

```bash
gh issue edit <n> --add-assignee @me \
  --add-label "status:in-progress" --remove-label "status:ready"
# Same PR — edit docs/architecture/_slices/<slice-id>.md:
#   status: ready → in-progress
#   owner: unassigned → <your-agent-id>   # e.g. codex-gpt5, gemini-3-1, cursor-claude
```

### Handoff (in-progress → in-review)

```bash
gh issue edit <n> --add-label "status:in-review" --remove-label "status:in-progress"
# Same PR — frontmatter status: in-progress → in-review
# Also: mark PR ready for review: gh pr ready <m>
```

### Done (merge)

PR body contains `Closes #<n>`; merge auto-closes the Issue. Flip slice frontmatter `status: in-review → done` in the same PR (or a tiny follow-up if the review happens after merge).

### Block

```bash
gh issue edit <n> --add-label "status:blocked"
gh issue comment <n> --body "Blocked on <reason + cross-slice ticket link>"
# Frontmatter status: <previous> → blocked, append work-log entry.
```

### Enforcement

- `make issue-check` (or `make agent-check`, which includes it) cross-references frontmatter against Issue labels and reports drift. PR reviewers treat drift as a must-fix.
- Renames / splits / retires of slices use the `slice-reconcile` skill (`.agents/skills/slice-reconcile/SKILL.md` for Codex and similar; `.claude/skills/slice-reconcile/` for Claude). Manual one-sided edits are merge-blockers.

## The workflow (seven steps, apply verbatim)

1. **Pick a slice.** `gh issue list --label "slice,status:ready"` — Issues with that pair are claimable today (all `depends-on` slices done or first-cut-merged enough to depend on). Don't invent a different process.
2. **Claim atomically** (Dual-update rule above, §Claim). Re-read the Issue immediately after — if you see multiple assignees, step down and pick a different slice.
3. **Branch:** `git switch -c slice/<slice-id>` off `v2`. Push immediately with `-u`.
4. **Open a Draft PR** with `Closes #<n>` in the body. Do this before writing any code — it makes work-in-progress visible so other agents don't start the same slice.
5. **Write the test file.** First commit: `test(<scope>): initial test contract for <slice-id>`. Every Test Contract bullet appears as a function (closure rule §1); items deferred to later slices appear as `@pytest.mark.skip(reason="deferred to slice-…")` with a named follow-up. Tests should fail — that's expected at this stage.
6. **Implement.** Respect `forbidden_paths`. Every mutation at a module boundary returns `Result[T, E]` — not raised exceptions. No `except Exception: pass`. No silent `time.sleep()`. No `os.environ` reads outside `src/musubi/config.py`. Use `batch_update_points` on Qdrant, never loop `set_payload`.
7. **Verify + hand off:**
   ```bash
   make check           # ruff format + lint + mypy --strict + pytest + coverage ≥ 85%
   make agent-check     # vault frontmatter + slice DAG + spec hygiene + issue drift
   ```
   Then: flip slice frontmatter + Issue label (Dual-update rule §Handoff), append a work-log entry to your slice note with diff summary + Test Contract coverage matrix, run `gh pr ready <m>`.

## Self-review before opening PR

Paste this into your PR description (the template has it):

```
| Test Contract bullet | State | Evidence |
|---|---|---|
| test_foo_does_bar | ✓ passing | tests/module/test_foo.py:42 |
| test_baz_edge_case | ⏭ skipped (slice-xyz: <reason>) | tests/module/test_foo.py:110 |
| test_out_of_scope | ⊘ declared out-of-scope | _slices/<id>.md#Work log |
```

One row per spec Test Contract bullet. Silent omissions get request-changes.

### Before handoff — the five checks

Before flipping slice `in-progress → in-review` and marking a PR ready-for-review, run and carefully read the output of each:

1. **`make check`** — ruff format + lint (whole repo, matches CI) + mypy strict + pytest + coverage. Must exit 0.
2. **`make tc-coverage SLICE=<slice-id>`** — Closure Rule audit. Must exit 0.
3. **`make agent-check`** — vault-hygiene audit. **Distinguish `✗` errors from `⚠` warnings.** Exit non-zero? Grep for `✗` first — don't wave off a pre-existing warning.
4. **`gh pr checks <pr-number>`** — remote CI state. Local-green + remote-red means tooling drift; stop and diagnose, do not `--admin` past it.
5. **PR body linkage:**
   - Slice PRs: first line of the body is `Closes #<issue-number>.` (exact keyword, case-insensitive: `Closes` / `Fixes` / `Resolves` — prefer `Closes`). Without it GitHub doesn't auto-link and the Issue stays open after merge.
   - Chore / infra / docs PRs with no tracking Issue: include a line `No tracking Issue: <one-sentence reason>` so the absence is deliberate.

### Additional handoff-readiness rules

- **Symmetric coverage.** A class / function / module that promises X and Y in its docstring needs tests for both. Defensive-branch exceptions apply only to validation + error paths, never to advertised features.
- **ADR-punted dependencies must fail loud.** If you defer a dependency behind an ADR, the production path must `raise NotImplementedError` or log at `ERROR`/`CRITICAL` with an explicit stub message. `info` logs are not safety gates.
- **PR body reflects shipped code.** If the design evolved during implementation, update the PR description before marking ready-for-review. Don't make the reviewer reconcile stale intent against actual behaviour.

## Hard prohibitions (automatic revert)

- Silent `time.sleep()` in production code (async waits + timeouts only).
- `os.environ` reads outside `src/musubi/config.py`.
- Hardcoded hosts, ports, collection names, thresholds. (Hostnames + IPs especially — see `.agent-context.local.md` placeholder scheme.)
- New top-level dependencies without an ADR in `docs/architecture/13-decisions/`.
- `except Exception: pass`.
- `git push --force` on shared branches; `--no-verify` on commits.
- **Silently deferring a Test Contract bullet** — see Closure Rule.
- **Punting a method to a slice that doesn't own its code path** — see Method-ownership rule.
- **Flipping slice frontmatter without flipping the Issue label** (or vice versa) — see Dual-update rule.
- Committing anything in `.agent-context.local.md`, `.agent-brief.*.local.md`, `.env.local`, `.secrets/`, or matching `*.pem` / `*.key` / `id_*`.

## Style (enforced by linters + CI)

- **Python 3.12.** strict mypy. ruff format + check. pydantic v2 models for every payload; dicts only at the Qdrant boundary.
- **Errors:** `Result[T, E]` at module boundaries. Typed error dataclasses. Unhandled errors become 5xx with correlation IDs at the API layer.
- **Async surface.** Internal sync OK if no I/O.
- **Structured JSON logs**, one field per concept. Never f-string a log message. Correlation IDs propagate.
- **No `print()`. Ever.**
- **Import discipline:** `sdk/*` imports `types/*` only. `adapters/*` imports `sdk + types` only. `api/*` composes `planes/*` + `retrieve/*` + `lifecycle/*`. Violations fail `make check` (import-linter check — future addition; enforced by review today).
- **Conventional Commits.** `feat(scope): …`, `fix(scope): …`, `test(scope): …`, `docs(scope): …`, `chore(scope): …`, `refactor(scope): …`. Same-PR spec changes get a `spec-update: <doc-path>` trailer.
- **Test function names transcribe spec Test Contract bullet text verbatim.**

## Agent identification

When you append to a slice's `## Work log` or claim an Issue, use an agent id that starts with your tool family:

- `codex-<model>` (e.g. `codex-gpt5`)
- `gemini-<version>` (e.g. `gemini-3-1`)
- `cursor-<backing-model>` (e.g. `cursor-claude`)
- `grok-<version>`
- `cowork-<operator-id>`
- `claude-<interface>-<model>` (e.g. `claude-code-opus47`)

This lets humans + other agents see at a glance which tool shipped which work.

## When you're stuck

1. Don't guess. Don't "just make it work."
2. Drop a file at `docs/architecture/_inbox/questions/<slice-id>-<slug>.md`: goal, expectation, observation, options.
3. Flip your slice to `blocked` on both sides (Dual-update rule §Block).
4. Comment the Issue with a link to the question file.
5. Pick another slice — don't hold a lock while stuck.

## Definition of Done (from `docs/architecture/00-index/definition-of-done.md`)

- [ ] Every Test Contract bullet in closure state 1, 2, or 3 (see above).
- [ ] Coverage ≥ 85 % on owned files (≥ 90 % on planes/** + retrieve/**).
- [ ] `make check` green (ruff format --check + ruff check + mypy strict + pytest + coverage `fail_under=85`).
- [ ] `make agent-check` green (vault health + issue drift).
- [ ] Spec files updated if prose changed (`spec-update:` trailer on the commit).
- [ ] Frontmatter + Issue both flipped to `in-review` (then `done` at merge).
- [ ] A *different* agent or a human reviews + merges. No self-approval.
- [ ] Work-log entry on the slice note; cross-ref to `docs/architecture/00-index/work-log.md` if the slice realised a spec milestone.

## Why this file is long

Because when you are not Claude Code, you do not have a tuned sub-agent system prompt carrying the context for you. This file *is* your system prompt. Read it every time. The Dual-update rule, the Closure Rule, and the Method-ownership rule are the ones most likely to trip a first-time agent — the other rules are mostly the usual Python hygiene.

## For reference only (do not edit as an agent)

- `CLAUDE.md` — Claude-specific entry point; same contract.
- `docs/architecture/00-index/agent-guardrails.md` — authoritative expansion of every rule here, plus vault rules (Obsidian, curated plane writes). Read if you're working on vault-sync / curated plane.
- `docs/AGENT-PROCESS.md` — multi-agent concurrency model: branch naming, review etiquette, concurrency gotchas.
- `docs/architecture/00-index/conventions.md` — the full style guide, frontmatter schema, tag taxonomy.

If any of those contradict this file, ask before acting — contradictions are bugs, not features.
