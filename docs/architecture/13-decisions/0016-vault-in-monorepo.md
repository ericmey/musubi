---
title: "ADR 0016: Obsidian vault lives in the monorepo at `docs/architecture/`"
section: 13-decisions
type: adr
status: accepted
date: 2026-04-18
deciders: [Eric]
tags: [section/decisions, status/accepted, type/adr, monorepo, vault, agents]
updated: 2026-04-18
up: "[[13-decisions/index]]"
reviewed: true
supersedes: "[[13-decisions/0015-monorepo-supersedes-multi-repo]] (vault-separate clause only)"
superseded-by: ""
---

# ADR 0016: Obsidian vault lives in the monorepo at `docs/architecture/`

**Status:** accepted
**Date:** 2026-04-18
**Deciders:** Eric

## Context

[[13-decisions/0015-monorepo-supersedes-multi-repo]] folded core + SDK + adapters + infra + contract tests into a single repo, but kept the Obsidian architecture vault out as a "human-authored" separate repo. That decision was made when there was one developer and one agent working serially.

The situation changed quickly:

- V2 development is about to spin up **multiple concurrent coding agents** (Claude Code, Claude Cowork, Codex, and occasionally Grok / Gemini) working on different slices.
- Every agent, regardless of provider, needs to read the same specs, follow the same guardrails, and update the same slice-state notes to pick up work safely.
- Keeping the vault in a sibling directory (`~/Vaults/musubi/` outside the code repo) meant each agent needed two clones, cross-repo references, and its own "how to find the vault" onboarding.
- PRs that correct a spec alongside code (the `spec-update:` trailer convention from [[CLAUDE]]) had to touch two repos with no atomic landing point.

Moving the vault into the code repo removes all of that friction, at the cost of mixing human-authored Markdown with generated code changes in a single commit graph. At the scale of one-developer-many-agents, the cost is small; the benefit is large.

## Decision

The Obsidian architecture vault becomes a first-class directory inside the monorepo:

```
~/Projects/musubi/                 ← the repo (github.com/ericmey/musubi)
├── src/musubi/                    ← code
├── tests/
├── docs/architecture/             ← the vault (this ADR's subject)
│   ├── 00-index/
│   ├── 01-overview/
│   ├── …
│   ├── _slices/
│   ├── _inbox/
│   ├── _templates/
│   ├── _bases/
│   ├── _attachments/
│   └── .obsidian/                 ← plugin config + types.json checked in
├── .claude/                       ← Claude Code agent + skill definitions
├── .cursor/, AGENTS.md, GEMINI.md ← shims for other coding agents
├── .github/                       ← PR + issue templates
└── CLAUDE.md                      ← root agent entry point
```

- The vault is **moved**, not copied: `~/Vaults/musubi/` is retired in favour of `~/Projects/musubi/docs/architecture/`. Existing Obsidian workspaces are re-pointed at the new path.
- `.obsidian/` is **mostly checked in** so plugin state (Breadcrumbs fields, Linter config, Templater templates, Bases definitions, property types) travels with the repo. The `.gitignore` excludes transient per-machine state: `workspace.json`, `workspaces.json`, `cache/`, `*.bak`, `.trash/`.
- Obsidian's Local REST API plugin remains configured per-user; no secrets land in git.

## Consequences

### Positive

- **One clone to rule them all.** A fresh agent runs `git clone git@github.com:ericmey/musubi.git && cd musubi` and has the code, the specs, the slice registry, the agent guardrails, the test fixtures catalog, and every ADR. There is no "and also go clone the vault" step.
- **Atomic spec + code PRs.** The `spec-update: <doc-path>` commit trailer finally means something — both files live in the same git history and a single PR review covers both.
- **CI can enforce vault gates.** A `vault-check.yml` GitHub Action runs the existing `make agent-check` / `slice-check` / `spec-check` targets against every PR; the frontmatter linter, slice DAG validator, and Test Contract hygiene become PR-blocking instead of operator-discipline.
- **Multi-agent coordination gets simpler.** Every agent — regardless of vendor — reads the vault from `docs/architecture/` via the same repo clone. Path conventions are the same for Claude Code, Codex, Gemini CLI, and Cursor.
- **No cross-repo versioning.** The spec I write today and the code I write tomorrow are the same sha; anyone reading history can reconstruct "what did we believe when we built this?" by checking out a single commit.

### Negative

- **Mixed commit graph.** A "fix typo in 05-retrieval" commit lives alongside "feat(retrieve): hybrid scoring." Mitigation: Conventional Commits with `docs:` vs `feat:`/`fix:` prefixes already separates them visually in `git log --oneline`.
- **Obsidian plugin churn could noise the repo.** On-save linting can produce frontmatter key-reorder commits if plugin versions drift between machines. Mitigation: the vault's obsidian-linter config sorts keys deterministically; any drift shows up in a PR diff and is easy to catch. If noise becomes a problem, we add a pre-commit hook that strips the linter's autosort from non-docs-changing PRs.
- **Attachments could bloat history.** `_attachments/` today is small; if agents start dropping large screenshots or recordings, we move that dir to git-lfs. Tracked as a follow-up.
- **Nested `.git`-inside-vault hazard during the migration.** Obsidian was previously backed by its own git repo; during the move that nested `.git/` was removed before committing. Documented in [[00-index/work-log]].

### Neutral

- **`_inbox/locks/` stays in-repo but becomes a secondary mechanism.** Primary coordination moves to **GitHub Issues** (one issue per active slice, assignee = owner, `status:*` labels) — see [`docs/AGENT-PROCESS.md`](../../AGENT-PROCESS.md) (one level up from the vault) for the full model. Agents still drop a `.lock` file for belt-and-braces but the authoritative lock is the Issue assignee.
- **Nothing about the vault's content model changes.** All wikilinks remain relative; section numbering stays; frontmatter schema is unchanged. The move is purely where-it-lives.

## Alternatives considered

### A) Keep 0015's "vault stays separate" posture

Rejected. Two-clone onboarding across four+ agent vendors is real recurring friction. The only argument for separation was "Obsidian plugin churn" which the on-save linter has already neutralised.

### B) Vault as a git submodule under `docs/architecture/`

Considered. Preserves the two-repo boundary while presenting a single clone. Rejected because submodules are notoriously fragile for concurrent contributors — every agent would need to know how to update and commit the submodule pointer, adding exactly the kind of per-agent training this move is meant to eliminate.

### C) Vault as a git subtree

Considered. Less friction than submodule, but still requires every agent to know the subtree dance on pushes back. Same rejection reason.

### D) Publish the vault as a read-only GitHub Pages site; agents consume via HTTP

Rejected. Specs are load-bearing and agents need to write to `_slices/<slice>.md` to flip status. Read-only is wrong.

## References

- [[13-decisions/0015-monorepo-supersedes-multi-repo]] — updated to note this follow-on.
- [`docs/AGENT-PROCESS.md`](../../AGENT-PROCESS.md) — multi-agent concurrency model (Issues as the lock board). *Outside the vault; Obsidian wikilinks don't resolve it.*
- Root `CLAUDE.md` (repo root) — agent entry point; points at `docs/architecture/` for specs.
- [[00-index/agent-guardrails]], [[00-index/agent-handoff]] — unchanged in content; paths updated to reflect the new layout.
- Migration commit: see [[00-index/work-log#2026-04-18 — Vault moved into the monorepo]].
