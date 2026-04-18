# Agent skills for Musubi (non-Claude agents)

These are the skill definitions non-Claude coding agents discover at session start — Codex, Aider, Continue, Crush, Cline, and anything else that reads `.agents/skills/<name>/SKILL.md`. Claude Code uses the parallel tree at `.claude/skills/`.

## Current skills

| Skill | Purpose | Mirror in `.claude/skills/` |
|---|---|---|
| `pick-slice` | Find a `status:ready` slice, claim it via the Dual-update rule, branch, open draft PR. | ✓ |
| `handoff` | Verify DoD + Test Contract closure, flip state to `in-review`, mark PR ready. | ✓ |
| `spec-check` | Run vault-hygiene gates (`make agent-check`) + generate the Test Contract coverage matrix for the PR. | ✓ |

## Mirror pattern with `.claude/skills/`

Claude Code and the non-Claude toolchains look in different directories for their skills, so the same workflow exists in two places. The **substance is identical** except for single-line tool-aware cross-references (this mirror points at `AGENTS.md`; Claude's version points at `CLAUDE.md`).

### Rules for keeping them in sync

Same as in [`../.claude/skills/README.md`](../../.claude/skills/README.md) — copying here so non-Claude agents aren't asked to read Claude's tree:

1. Edit both files simultaneously when changing a workflow.
2. Diff them with `diff -u .claude/skills/<name>/SKILL.md .agents/skills/<name>/SKILL.md` — the only difference should be the intentional tool-aware cross-reference line.
3. Commit both in the same PR.

### Adding / removing / renaming a skill

1. Apply the change in both directories in the same commit.
2. Update the tables in both `.claude/skills/README.md` and `.agents/skills/README.md`.
3. Update `CLAUDE.md` and `AGENTS.md` if the skill is named there.

### Why not unify to one source-of-truth?

Current divergence between pairs is 1 line each (tool-specific reference). Maintaining a renderer / symlinks would cost more than the pair-edit discipline. Re-evaluate when skill count grows past ~10 or per-tool divergence starts mattering.

## Where the authoritative rules live

- Universal rules: `docs/architecture/00-index/agent-guardrails.md`.
- Multi-agent coordination: `docs/AGENT-PROCESS.md`.
- Non-Claude entry point: `AGENTS.md` at the repo root. (Gemini CLI: `GEMINI.md`. Cursor: `.cursor/rules/musubi.mdc`. Claude Code: `CLAUDE.md`.)

Skills implement *workflows*, not *rules*. Guardrails win any conflict.
