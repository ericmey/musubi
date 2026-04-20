---
name: handoff
description: Move a slice from `in-progress` to `in-review` — verify Definition of Done, mark PR ready for review, update labels, append the work-log entry. Use when the implementation + tests are done and the PR is ready for another agent to review.
---

# Skill: handoff

Transition one in-flight slice to review-ready. This is the opposite bookend to `pick-slice` — you took the slice, you did the work, now you're handing it off.

## Rules this skill enforces

- **Test Contract Closure Rule** (see `docs/Musubi/00-index/agent-guardrails.md#Test-Contract-Closure-Rule` and [AGENTS.md](../../../AGENTS.md)) — every bullet in every relevant spec's `## Test Contract` section must be in one of three states at handoff: passing test with verbatim name / skipped with reason / declared-out-of-scope in the slice work log. This skill surfaces silent omissions; don't suppress them.
- **Dual-update rule** — handoff flips BOTH the GitHub Issue (`status:in-progress → status:in-review`) AND the vault slice file's frontmatter in the same PR. `make issue-check` catches any drift.
- **No self-approval** — handoff marks the PR ready for review; a *different* agent (or a human) approves + merges. If you push a commit in response to review, a different reviewer takes the next pass.

## When to invoke

- User says: "hand it off", "this is ready for review", "close it out", `/handoff`.
- After you've pushed your final implementation commit for a slice and `make check` is green.

## Do not invoke when

- Tests are still red.
- You haven't run `make check` end-to-end this session.
- You have uncommitted changes in the working tree.
- The slice's Definition of Done has unresolved boxes.

## Instructions

### 1. Confirm the preconditions

```bash
# Working tree is clean
git status --porcelain
# Expect: empty output. If not, stop — commit or discard first.

# On the slice branch
git branch --show-current
# Expect: slice/<slice-id>

# Tests + lint + typecheck + coverage
make check
# Expect: all green. If not, stop — fix, don't hand off broken code.
```

If any check fails → do not proceed. Tell the user what's failing.

### 2. Re-read the Definition of Done

Open `docs/Musubi/00-index/definition-of-done.md` and the slice's own Definition-of-Done section. Verify each bullet — walk the list aloud in your reply so the user can see you checked:

- [ ] Every Test Contract item in the linked spec(s) is a passing test (or explicitly deferred with a named follow-up slice).
- [ ] Branch coverage ≥ 85 % on owned paths (90 % for `planes/**` / `retrieve/**`).
- [ ] Spec `status:` updated if prose changed (`spec-update:` trailer present in a commit).
- [ ] Work-log entry appended to the slice note.
- [ ] Imports honour the discipline from ADR 0015 (sdk only imports types; adapters only import sdk+types; api composes the rest).

If any bullet is not ticked → stop, ask the user whether to finish it or defer (with a reason).

### 3. Append the work-log entry

Edit `docs/Musubi/_slices/<slice-id>.md` under `## Work log`:

```markdown
### YYYY-MM-DD HH:MM — <agent-id> — handoff to in-review

- `<one-line summary of what landed>`.
- Tests: <n> passing (covers <m>/<total> Test Contract bullets; deferred: <list or "none">).
- Coverage: <pct> % on owned paths.
- `make check` clean: ruff format + lint + mypy strict + pytest.
- PR #<m> marked ready for review.
```

Commit as `docs(slice): handoff <slice-id> to in-review`.

### 4. Flip the slice frontmatter

- `status: in-progress` → `status: in-review`
- `updated: <today>`

Include this in the same commit as step 3 if you haven't pushed yet.

### 5. Append to the project work-log

If this slice realises a spec (most do), add an entry to `docs/Musubi/00-index/work-log.md`. Keep it short; the detail is in the slice note.

```markdown
### YYYY-MM-DD — <slice-id> first cut ready for review

One paragraph on what landed and what it unblocks. Point at the slice note for detail.

Vault changes:
- [[_slices/<slice-id>]] — `status: in-progress → in-review`.
- <any spec files edited via `spec-update:` trailer>
```

Commit alongside step 3 or as a separate `docs(work-log): <slice-id> ready for review` commit.

### 6. Push + mark PR ready

```bash
git push
gh pr ready <m>
gh pr edit <m> --remove-label "status:in-progress" --add-label "status:in-review"
gh pr comment <m> --body "Ready for review. Recommending \`musubi-reviewer\` subagent for the first pass."
```

### 7. Remove the file-based lock

```bash
git rm docs/Musubi/_inbox/locks/<slice-id>.lock
git commit -m "chore(lock): release <slice-id> — handed off to in-review"
git push
```

Primary lock is now the `status:in-review` label + assignee on Issue #n.

### 8. Report

One or two lines back to the user: "`<slice-id>` handed off. PR #<m>, slice `status: in-review`, Test Contract <n>/<total> covered, coverage <pct> %. `musubi-reviewer` can take it from here."

## If review comes back with changes requested

- Flip slice back to `status: in-progress`.
- Re-add `status:in-progress` label to the PR.
- Address the review comments.
- Run through this skill again when ready.

## Don't do

- **Don't merge your own PR.** A different human or agent approves + merges.
- **Don't squash the branch history yourself.** Review the PR's commit graph; squash-at-merge is the repo's merge strategy if that's what's configured.
- **Don't silence failing tests.** If a test is flaky, mark it `@pytest.mark.flaky` with a linked issue and explain in the review request — never just delete it.
