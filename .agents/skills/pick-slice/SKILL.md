---
name: pick-slice
description: Find the next unclaimed ready-to-work slice in the Musubi vault, claim it atomically via GitHub Issue assignment, lock it, and prepare a working branch + draft PR. Use at the start of any coding session when the user hasn't specified which slice to work on.
---

# Skill: pick-slice

Select the next slice from `docs/Musubi/_slices/` that is `status: ready`, has all `depends-on` slices at `status: done` (or `in-progress` with their blocking surface landed), and has no active GitHub Issue assignee.

## Rules this skill enforces

- **Dual-update rule** (see `docs/Musubi/00-index/agent-guardrails.md#Dual-update-rule` and [AGENTS.md](../../../AGENTS.md)) — claiming a slice flips BOTH the GitHub Issue (assignee + `status:in-progress` label) AND the vault slice file's frontmatter (`status`, `owner`) in the same PR. This skill walks both updates; don't skip either half.
- **Atomic-claim rule** — GitHub Issue `--add-assignee` is the authoritative lock; re-read the Issue immediately after to detect races. Vault-file + lock-file edits are the *secondary* record.

## When to invoke

- User says: "pick a slice", "what's next", "grab one off the board", or `/pick-slice`.
- Session-start hook after cloning a fresh worktree.
- After completing one slice and user says "keep going".

## Do not invoke when

- The user named a specific slice. Work on that one — don't second-guess them.
- The user is asking a question about *what* slices exist (read-only — no claim).

## Instructions

### 1. Scan the slice registry

```bash
cd ~/Projects/musubi  # or wherever pwd puts you
grep -l "^status: ready" docs/Musubi/_slices/slice-*.md
```

For each candidate, open the file and check:

- `depends-on:` — every listed slice must be `status: done` OR `status: in-progress` with a first-cut already merged (read the work log section).
- The slice's block list — higher-block-count = higher priority (unblocks more downstream work).

### 2. Check GitHub for existing claims

```bash
gh issue list --label "slice" --state open --json number,title,assignees,labels
```

If an issue exists for your candidate slice and has an assignee → skip, pick the next candidate.
If an issue exists but is unassigned → claim it (step 3).
If no issue exists → create one (step 3 alt).

### 3. Claim atomically

**If an Issue already exists (unassigned):**

```bash
gh issue edit <n> --add-assignee @me --add-label "status:in-progress" --remove-label "status:ready"
```

Race condition check: immediately re-read the issue. If `assignees` has multiple names, another agent also claimed it — apologise in an issue comment, remove your assignee, pick a different slice.

**If no Issue exists for this slice:**

```bash
gh issue create \
  --title "slice: <slice-id>" \
  --label "slice,status:in-progress" \
  --assignee @me \
  --body "Tracks implementation of [docs/Musubi/_slices/<slice-id>.md](docs/Musubi/_slices/<slice-id>.md).

Owns paths, specs, Test Contract: see the slice note."
```

### 4. Branch + draft PR

```bash
git switch main
git pull --ff-only
git switch -c slice/<slice-id>
# Initial empty commit so the PR can open before any file changes.
git commit --allow-empty -m "chore(slice): take <slice-id>

Claims docs/Musubi/_slices/<slice-id>.md. See issue #<n>."
git push -u origin slice/<slice-id>
gh pr create --draft \
  --base main \
  --title "feat(<scope>): <slice-id>" \
  --body "Closes #<n>. Draft — work in progress per docs/Musubi/_slices/<slice-id>.md."
```

### 5. Flip slice frontmatter

Edit `docs/Musubi/_slices/<slice-id>.md`:

- `status: ready` → `status: in-progress`
- `owner: unassigned` → `owner: <your-agent-id>` (e.g., `eric-cc-opus47`, `yua-cowork`, `codex-gpt5`)
- Append a work-log entry under `## Work log`:

  ```markdown
  ### YYYY-MM-DD HH:MM — <agent-id> — claim

  - Claimed slice via `pick-slice` skill. Issue #<n>, PR #<m> (draft).
  ```

Commit as `chore(slice): flip <slice-id> to in-progress`.

### 6. Drop the file-based lock (belt-and-braces)

```bash
touch docs/Musubi/_inbox/locks/<slice-id>.lock
echo "<agent-id>  $(date -Iseconds)  PR #<m>" > docs/Musubi/_inbox/locks/<slice-id>.lock
git add docs/Musubi/_inbox/locks/<slice-id>.lock
git commit -m "chore(lock): <slice-id>"
```

(Primary lock is the Issue assignee; this file is secondary and makes local-only agents visible to git-only reviewers.)

### 7. Report

One sentence back to the user: "Claimed `<slice-id>`. Issue #<n>, branch `slice/<slice-id>`, draft PR #<m>. Starting with the Test Contract." Then proceed to write tests per the slice spec — that's the next step of the slice-worker loop, not part of this skill.

## Conflict resolution

- **Two agents claimed the same issue at the same time** → the one whose `gh issue edit --add-assignee` succeeded keeps it; the other observes multiple assignees on re-read and steps back. If both show as assignees, the one with the **later** `createdAt` on the self-assignment event yields. (GitHub Issue event log tells you who was first.)
- **Depends-on slice isn't actually ready but slice said ready** → mark the candidate `status: blocked`, file a question in `docs/Musubi/_inbox/questions/`, pick a different slice.
- **No ready slices are available** → tell the user. Don't make up work.
