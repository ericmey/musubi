---
title: Agent Handoff Protocol
section: 00-index
type: index
status: complete
tags: [section/index, status/complete, type/index]
updated: 2026-04-17
up: "[[00-index/index]]"
reviewed: true
---

# Agent Handoff Protocol

How coding agents coordinate without stepping on each other. This is a *protocol*, not a suggestion — violating it produces merge conflicts, duplicated abstractions, and drift. Also see [[00-index/agent-guardrails]].

## Lifecycle of a slice

```
    pick up ───▶ lock ───▶ plan ───▶ test ───▶ code ───▶ review ───▶ done
       │                                                                │
       └────── (blocked) ─── cross-slice ticket ──── (resolved) ────────┘
```

### 1. Pick up

- Browse [[_slices/index]] or open [[12-roadmap/slice-board]] (Kanban).
- Choose a slice with `status: ready` whose **depends-on** slices are all `status: done`.
- Avoid slices where all files under `owns_paths` have a lock present (see 2).

### 2. Lock

- Create `_inbox/locks/<slice-id>.lock` containing:
  ```
  agent_id: <your handle>
  started_at: <ISO8601 UTC>
  pr_branch: <branch-name-if-known>
  ```
- Flip the slice's frontmatter `status` to `in-progress`, set `owner` to your agent id.
- Update `_inbox/locks/<slice-id>.lock`'s mtime every 30 minutes (heartbeat). A lock > 4h old without a heartbeat is stale and may be taken over.

### 3. Plan

- Read the slice note's linked specs top-to-bottom.
- Open the section's `CLAUDE.md` for local conventions.
- Extract the **Test Contract** — each item becomes a pending test.
- If anything is ambiguous, create `_inbox/questions/<slice-id>-<slug>.md` and block; don't guess.

### 4. Test (first commit)

- Write the test file **before** the implementation. Every `- [ ]` item in the Test Contract becomes a test name.
- Test names are assertions: `test_fast_path_excludes_provisional_memories`, not `test_fast_path_1`.
- Commit with the message `test(<scope>): initial test contract for <slice-id>`.

### 5. Code

- Implement until all Test Contract tests pass.
- Stay inside `owns_paths`. If you need to touch a `forbidden_paths` file, **stop** and open a cross-slice ticket (see 7).
- PR size cap: 800 lines of code (excluding generated code and fixtures).

### 6. Review

- Flip slice `status` to `in-review`.
- Append PR link to the slice's **PR links** section.
- Check [[00-index/definition-of-done]] line-by-line before requesting review.
- Any CI failure reverts you to step 5.

### 7. Cross-slice coordination

If your slice needs a change outside `owns_paths`:

- **Do not edit the foreign file.**
- Create `_inbox/cross-slice/<slice-id>-<target>.md`:
  ```markdown
  ---
  from: <slice-id>
  to: <slice-id or path>
  status: open
  ---
  # What I need

  # Why

  # Proposed change (optional)
  ```
- Flip your slice's `status` to `blocked`.
- Pick up another slice while you wait.
- When the target slice (or a meta-agent) closes the ticket, flip back to `in-progress`.

### 8. Done

- All Definition of Done checkboxes green.
- Slice `status` → `done`.
- Lock file removed.
- PR merged.
- Downstream slices (`blocks`) are now eligible to start.

## Lock, ticket, and question inbox

| Folder                     | Purpose                                    | Who writes | Lifecycle           |
|----------------------------|--------------------------------------------|------------|---------------------|
| `_inbox/locks/`            | one `<slice-id>.lock` per active slice     | the working agent | exists during work; removed at PR merge |
| `_inbox/cross-slice/`      | coordination requests between slices       | any blocked agent | resolved by meta-agent or target-slice agent |
| `_inbox/questions/`        | agent-filed blockers (ambiguous spec, missing test fixture, etc.) | any agent | triaged daily; answered by a human or closed with a spec update |
| `_inbox/research/`         | open research questions (graduate from operator-notes) | human or agent | resolved by producing an ADR or folding answer into a spec |

## Status transitions

Slice `status` values and their transitions:

```
    ready ───▶ in-progress ───▶ in-review ───▶ done
       ▲           │               │
       │           ▼               │
       └───── blocked ◀────────────┘
```

| From        | To           | Trigger                                                          |
|-------------|--------------|------------------------------------------------------------------|
| `ready`     | `in-progress`| Agent takes the lock and makes first commit.                      |
| `in-progress` | `in-review`| PR opened; all Test Contract tests pass locally.                  |
| `in-review` | `done`       | PR merged; CI green; Definition of Done checklist complete.       |
| `in-progress` / `in-review` | `blocked` | Cross-slice ticket opened; waiting on external change.          |
| `blocked`   | `in-progress`| Ticket resolved.                                                  |
| any         | `ready`      | Rollback or reassignment. Agent hands off voluntarily.            |

## Etiquette

- **Don't split a slice mid-flight.** If you realize the slice is too big (> 800 LOC), stop, open a cross-slice ticket proposing a split, and wait for a human to update the registry.
- **Don't rename a file in another slice's `owns_paths`** just because your IDE suggested it. That's a cross-slice change.
- **Comment with the why, not the what.** Don't explain what a function does; explain a non-obvious constraint.
- **Never run `git push --force` on a shared branch.** If you need to rewrite history, open a new branch.

## Related

- [[00-index/agent-guardrails]] — the rules.
- [[00-index/definition-of-done]] — the checklist.
- [[_slices/index]] — the registry.
- [[12-roadmap/slice-board]] — live board.
