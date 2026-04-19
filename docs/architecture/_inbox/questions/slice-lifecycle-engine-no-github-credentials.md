---
title: "slice-lifecycle-engine — Cowork sandbox lacks GitHub credentials"
section: _inbox
type: research-question
status: research-needed
slice: slice-lifecycle-engine
agent: cowork-auto
tags: [section/inbox, status/research-needed, type/research-question, blocker]
updated: 2026-04-19
---

# slice-lifecycle-engine — Cowork sandbox lacks GitHub credentials

## Goal

Take Issue #11 (slice-lifecycle-engine) per the session brief: atomic claim via
`gh issue edit 11 --add-assignee @me --add-label status:in-progress`, branch
+ draft PR, then test-first implementation under `src/musubi/lifecycle/` and
`tests/lifecycle/`.

## What I expected

The Cowork session would have either:

- A logged-in `gh` CLI (so `gh issue edit`, `gh pr create`, etc. work), and/or
- An SSH key authorized for `git@github.com:ericmey/musubi.git` (so
  `git push -u origin slice/slice-lifecycle-engine` works).

This is required by the brief's hard constraint #1 (Atomic claim via the
Dual-update rule) and by the seven-step workflow in `AGENTS.md`.

## What I observed

In the Cowork bash sandbox at `/sessions/.../mnt/musubi-cowork`:

- `which gh` → not present in the base image. I downloaded the static
  `gh_2.63.2_linux_arm64` release into `/tmp/` (works as a binary) but…
- `gh auth status` → `You are not logged into any GitHub hosts.`
- No `GH_TOKEN` / `GITHUB_TOKEN` in the environment.
- `ls ~/.config/gh/` → does not exist.
- `git ls-remote origin v2` → `Host key verification failed.` /
  `Could not read from remote repository.` (no SSH key, no known_hosts entry
  for `github.com`).

So the sandbox cannot perform the atomic Issue claim, cannot push the
`slice/slice-lifecycle-engine` branch, and cannot create the draft PR via
`gh pr create`. The brief's first constraint is unsatisfiable from inside
this sandbox as currently provisioned.

## Options I see

1. **Provision credentials into the Cowork sandbox.** Either:
   a. Mount Eric's `~/.config/gh/hosts.yml` (or a Cowork-scoped fine-grained
      PAT with `repo`, `issues`, `pull_requests` write) into the sandbox and
      put the token in `GH_TOKEN`.
   b. Mount Eric's SSH key (or a deploy key with push) and add `github.com`
      to `~/.ssh/known_hosts`. Less ideal — the brief's prohibitions list
      forbids committing `id_*` files, but that's about commits, not mounts.
   This is the cleanest fix and unblocks every future Cowork session.

2. **Cooperative handoff: I do code-only, Eric handles git/gh.** I write the
   tests and implementation locally on a new branch in the sandbox. Eric's
   parallel Claude Code session at `~/Projects/musubi/` (which has gh +
   SSH) does the atomic claim, the push, and the draft-PR creation. I keep
   committing to the local branch; Eric pushes periodically. Slower
   coordination loop, but it works today.

3. **Stop and reschedule.** Pick the slice up in a different agent
   environment that has credentials (Claude Code, Codex, etc.). Cowork
   takes a different slice that doesn't need GitHub coordination — but
   essentially every slice does, so this is a non-starter long-term.

## My recommendation

Option 1 (provision credentials) is the right long-term fix. For *this*
session, Option 2 (Eric does the GitHub side, I do the code) gets the
slice-lifecycle-engine work landed today without blocking on env changes.

## Status

- Slice frontmatter still says `status: ready`, `owner: unassigned` — I have
  NOT touched it, because flipping it without flipping the Issue label would
  itself be a Dual-update-rule violation.
- No claim attempted. No branch pushed. No PR opened.
- I have done the prep reads (CLAUDE.md, AGENTS.md, AGENT-PROCESS.md,
  agent-guardrails.md, definition-of-done.md, conventions.md, slice file,
  04-data-model/lifecycle.md, 06-ingestion/lifecycle-engine.md, existing
  types + episodic plane code).

Awaiting Eric's call.
