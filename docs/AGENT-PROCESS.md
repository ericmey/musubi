# Multi-agent process for Musubi

How a fleet of AI coding agents coordinates on this repo without stepping on each other's work. Designed to be tool-agnostic so Claude Code, Claude Cowork, Codex, Cursor, Gemini, and Grok all participate under the same rules.

## 1. One clone, many agents

Every agent works out of `~/Projects/musubi/` (or its worktree equivalent on the agent's machine). Code lives under `src/`, tests under `tests/`, and **the Obsidian architecture vault lives under `docs/architecture/`**. No agent has to discover a second repo or a sibling directory — one `git clone` gives it everything it needs. See [docs/architecture/13-decisions/0016-vault-in-monorepo.md](architecture/13-decisions/0016-vault-in-monorepo.md) for the rationale.

## 2. The coordination primitive is a GitHub Issue, not a file

Earlier iterations of this project used file-based locks under `docs/architecture/_inbox/locks/`. That mechanism still exists (belt-and-braces) but it is no longer authoritative. **GitHub Issues are the lock board.** Reasons:

- GitHub's assignment endpoint is atomic across agent machines — two agents can't both "win" it.
- Assignees and labels are visible in the GitHub UI without cloning the repo.
- Each agent's coding tool already has `gh` or a GitHub MCP — no new tooling to learn.

### The life of one slice of work

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 1. Slice is ready                                                         │
│    docs/architecture/_slices/slice-<id>.md  has  status: ready            │
│    GitHub Issue exists with labels [slice, status:ready] and no assignee. │
└──────────────────────────────────────────────────────────────────────────┘
                                 │
                                 │  (agent claims — atomic)
                                 ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 2. In progress                                                            │
│    Issue: assignee = agent-id, labels = [slice, status:in-progress]       │
│    Branch: slice/<id> pushed to origin                                    │
│    Draft PR: opened immediately with "Closes #<n>"                        │
│    Slice frontmatter: status: in-progress, owner: <agent-id>              │
│    Lock file: docs/architecture/_inbox/locks/<id>.lock  (secondary)       │
└──────────────────────────────────────────────────────────────────────────┘
                                 │
                                 │  (test-first, implementation, make check)
                                 ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 3. In review                                                              │
│    PR: ready for review (not draft), labels = [..., status:in-review]     │
│    Slice frontmatter: status: in-review                                   │
│    Slice work-log: entry with diff summary, coverage, Test Contract diff  │
│    Lock file: REMOVED                                                     │
└──────────────────────────────────────────────────────────────────────────┘
                                 │
                                 │  (reviewer approves, or requests changes → 2)
                                 ▼
┌──────────────────────────────────────────────────────────────────────────┐
│ 4. Done                                                                   │
│    PR: merged into v2 (or main post-V2-cutover)                           │
│    Issue: auto-closed by "Closes #<n>"                                    │
│    Slice frontmatter: status: done                                        │
│    Work-log: final entry noting merge + which downstream slices unblocked │
└──────────────────────────────────────────────────────────────────────────┘
```

## 3. Labels tell the story at a glance

The repo uses these labels exclusively for coordination:

| Label                | Meaning                                                              |
|----------------------|----------------------------------------------------------------------|
| `slice`              | This Issue tracks one slice. Every slice Issue has this.             |
| `status:ready`       | Unassigned, dependencies met, any agent may claim.                   |
| `status:in-progress` | An agent is actively coding. Do not start a parallel Issue.          |
| `status:in-review`   | PR is ready for review. `musubi-reviewer` agent (or a human) takes it. |
| `status:blocked`     | Waiting on a cross-slice dependency or an unanswered question.       |
| `cross-slice`        | Cross-cutting coordination Issue, not a slice Issue.                 |
| `spec`               | Spec / ADR change Issue; may or may not have an associated code PR.  |

A `status:*` label applies to exactly one of the five states. No two at once; no missing label.

## 4. Who should do what (agent selection)

Our fleet includes agents with different strengths. Route work accordingly.

| Agent                | Typical strength                                            | Best for                                                                                   |
|----------------------|-------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| **Claude Code (Opus)** | Deep multi-file edits, strict typing, careful test-writing. | Slice implementation for any `planes/`, `retrieve/`, `lifecycle/` slice. The default.     |
| **Claude Cowork**    | Long horizon, autonomous multi-day work.                    | Large multi-slice features (e.g., the hybrid-search stack end-to-end) that would bore a session. |
| **Codex (GPT-5)**    | Fast iteration, strong generic-Python patterns.             | Small slices, test-fixture work, CI tweaks. Also good for spike-then-throw-away prototypes. |
| **Cursor**           | Interactive + local in the editor.                          | Debugging, reading across the codebase, small refactors the human is driving.              |
| **Gemini (3.1)**     | Long context across the whole vault.                        | Spec revisions and ADR drafting where you want cross-slice impact analysis. Use the `musubi-spec-author` agent profile. |
| **Grok (4.2)**       | Alternative model family for comparison / backup.           | Second-opinion reviews, diversity when two other agents disagree.                          |

The agent you use is your choice as the operator. The rules above apply regardless of tool: claim an Issue, branch, tests first, `make check`, PR, hand off.

## 5. How to claim a slice (step-by-step)

This is the flow every agent follows. Claude Code users can invoke the `pick-slice` skill (`.claude/skills/pick-slice/`) which automates it. Other agents run the commands manually.

```bash
# 1. From the repo root, on a fresh v2 checkout
git switch v2 && git pull --ff-only

# 2. See what's available
gh issue list --label "slice,status:ready" --state open

# 3. Pick one and claim — atomic assignment
gh issue edit <n> --add-assignee @me \
  --add-label "status:in-progress" --remove-label "status:ready"

# Immediately re-read to detect a race:
gh issue view <n> --json assignees
# If multiple assignees, you lost — remove yourself and pick another:
gh issue edit <n> --remove-assignee @me --add-label "status:ready" --remove-label "status:in-progress"

# 4. Branch + draft PR
git switch -c slice/<slice-id>
git commit --allow-empty -m "chore(slice): take <slice-id>

Claims docs/architecture/_slices/<slice-id>.md. See issue #<n>."
git push -u origin slice/<slice-id>
gh pr create --draft --base v2 \
  --title "feat(<scope>): <slice-id>" \
  --body "Closes #<n>."

# 5. Flip slice frontmatter + secondary lock file
# (edit docs/architecture/_slices/<slice-id>.md; add lock file; commit)

# 6. Write the test file first.
# 7. Implement. Verify. Hand off.
```

## 6. How to hand off

When tests pass + `make check` is clean:

```bash
# Flip frontmatter to in-review, add work-log entry, remove lock.
# (edit docs/architecture/_slices/<slice-id>.md)
git commit -am "docs(slice): handoff <slice-id> to in-review"
git push
gh pr ready <m>
gh pr edit <m> --remove-label "status:in-progress" --add-label "status:in-review"
gh issue edit <n> --remove-label "status:in-progress" --add-label "status:in-review"
```

Claude Code users: `.claude/skills/handoff/` automates this.

## 7. Review

One agent ships; a *different* agent (or the human) reviews. The reviewer runs through the Definition of Done in the PR template, tests the diff locally if the change touches runtime behaviour, and either approves (`gh pr review <m> --approve`) or requests changes (`gh pr review <m> --request-changes --body ...`). Claude Code has a dedicated `musubi-reviewer` subagent tuned for this.

Rule: the same agent that made the last code commit does not self-approve. If you pushed a commit to address review, a different reviewer takes the next pass.

## 8. Branch + merge strategy

- `v2` is the active development branch (will become `main` after V2 is feature-complete — see [docs/architecture/13-decisions/0016-vault-in-monorepo.md](architecture/13-decisions/0016-vault-in-monorepo.md)).
- Feature branches are `slice/<slice-id>` — one slice per branch, no bundling.
- PRs merge into `v2` with **squash merge** so the branch history reads one slice = one commit on `v2`.
- `v2` is branch-protected once this first push lands: require PR, require CI passing (`CI` workflow + `Vault check` workflow), require CODEOWNERS approval for the locked paths.
- Force-push to `v2` is forbidden. Direct commits to `v2` are forbidden.

## 9. Commit style

Conventional Commits, strictly:

- `feat(<scope>): <subject>` — new functionality.
- `fix(<scope>): <subject>` — bug fix.
- `test(<scope>): <subject>` — test changes without production code impact.
- `docs(<scope>): <subject>` — vault or README changes.
- `chore(<scope>): <subject>` — slice claim / lock / CI / tooling.
- `refactor(<scope>): <subject>` — no behaviour change.

When a PR's commit changed a spec in the same diff, add a trailer:

```
feat(retrieve): hybrid scoring landed

Implements docs/architecture/05-retrieval/hybrid-search.md §Weighted score.

spec-update: docs/architecture/05-retrieval/hybrid-search.md
```

## 10. Concurrency gotchas

### Two agents pick the same Issue at once

Mitigated by (a) atomic `gh issue edit --add-assignee` and (b) the "re-read immediately" check in the claim flow. If two assignees appear, the one whose `createdAt` on the self-assignment event is **later** steps back. Don't argue; just yield.

### An agent abandons a slice (crash, timeout, network)

The Issue will still show that agent as assignee even though they're not working on it. Recovery:

1. Another agent notices the slice has been stuck in `status:in-progress` for >24h with no commits on `slice/<id>`.
2. Comment the Issue (`gh issue comment <n>`) naming the stuck slice and proposing reclaim.
3. Human or daily reaper removes the stale assignee, flips label back to `status:ready`.
4. Clean up the `slice/<id>` branch if it exists but has no useful work.

### Cross-slice dependency surfaces mid-work

1. Source agent files a cross-slice Issue using the template (`.github/ISSUE_TEMPLATE/cross-slice.md`).
2. Source slice flips to `status: blocked` (both frontmatter and Issue label).
3. Target slice's owner (if any) or the next free agent picks up the cross-slice Issue.
4. When resolved, source agent's assignee is notified and they re-pickup.

### Vault edits conflict

Simultaneous edits to `docs/architecture/00-index/work-log.md` or a slice note are the most likely conflict point. Guidance:

- Keep your vault edits in the same PR as the code that motivated them.
- If you need to append to the work-log while someone else is also in-flight: land your PR first, or rebase + re-append on merge.
- Never edit another active slice's `_slices/<id>.md` from your PR — open a cross-slice Issue instead.

## 11. What this file is not

- Not a replacement for the vault's `agent-guardrails.md` — that file owns the four non-negotiable engineering rules.
- Not a skill or agent definition — those live in `.claude/skills/` and `.claude/agents/`.
- Not a branching model reference — `v2` is the active branch; feature branches are per-slice; everything else is governed by GitHub branch protection. If you need more: [GitHub docs on branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/defining-the-mergeability-of-pull-requests/about-protected-branches).

## 12. TL;DR for a new agent

1. Clone: `git clone git@github.com:ericmey/musubi.git && cd musubi && git switch v2`.
2. Read `CLAUDE.md`, this file, and `docs/architecture/00-index/agent-guardrails.md`.
3. `gh issue list --label "slice,status:ready"` to see what's available.
4. Claim one. Branch. Draft PR. Tests first. Implement. `make check`. Mark ready.
5. A different agent or a human reviews. You don't self-approve.
6. After merge, your slice is `done` and the Issue closes. Pick the next.
