# Claude Code skills for Musubi

These are Claude Code's `.claude/skills/<name>/SKILL.md` definitions — slash-command-accessible workflows the Claude Code harness discovers at session start. Each skill encodes a recurring multi-step task so the agent doesn't reinvent it from scratch.

## Current skills

| Skill | Purpose | Mirror in `.agents/skills/` |
|---|---|---|
| `pick-slice` | Find a `status:ready` slice, claim it via the Dual-update rule, branch, open draft PR. | ✓ |
| `handoff` | Verify DoD + Test Contract closure, flip state to `in-review`, mark PR ready. | ✓ |
| `spec-check` | Run vault-hygiene gates (`make agent-check`) + generate the Test Contract coverage matrix for the PR. | ✓ |

## Mirror pattern with `.agents/skills/`

Non-Claude coding agents (Codex, Aider, Continue, Crush, etc.) read `.agents/skills/<name>/SKILL.md` at session start — different directory, same content. The pair exists because the two toolchains look in different places; the **substance is identical** except for single-line tool-aware cross-references (Claude's skill points at `CLAUDE.md`; the mirror points at `AGENTS.md`).

### Rules for keeping them in sync

When you edit one skill, you edit both. Pair edits:

1. Edit `.claude/skills/<name>/SKILL.md`.
2. Apply the same change to `.agents/skills/<name>/SKILL.md`, substituting the tool-specific cross-reference line (CLAUDE.md ↔ AGENTS.md) where relevant.
3. Run `diff -u .claude/skills/<name>/SKILL.md .agents/skills/<name>/SKILL.md` and verify the diff is ONLY the intentional tool-specific lines — no substance drift.
4. Commit both in the same PR.

### When to add a new skill

1. Create the skill in **both** directories simultaneously. Don't land one and "get to the other later" — that's the drift pattern.
2. Update this README's table + the parallel table in [`.agents/skills/README.md`](../../.agents/skills/README.md) to list the new skill.
3. If the skill warrants a mention in the top-level agent-entry docs ([`CLAUDE.md`](../../CLAUDE.md) and [`AGENTS.md`](../../AGENTS.md)), add it to both.

### When to *remove* or rename a skill

1. Remove (or rename) in both directories in the same commit.
2. Update both README tables.
3. If any agent definition or entry doc references the skill by name (`musubi-slice-worker.md` invokes the `pick-slice` skill, for example), update those references.

## Why not a single source-of-truth + rendering script?

Considered. Rejected for now because:

- The diff is truly small (currently 1 line per pair of files) and easy to manually replicate.
- A renderer would pull in extra build complexity and a separate "did you re-render after editing?" discipline that's arguably worse than the 5-line manual diff.
- We can revisit if the set of skills grows past ~10 or if the per-tool divergence grows materially.

A lightweight drift check could be added to `docs/architecture/_tools/check.py` in the future (flag if the body of a pair differs by >N substantive lines after stripping known tool-aware blocks). Not wired today; agent discipline + review carry it.

## Where the authoritative rules live

- Universal rules every agent on the project follows, regardless of skill invocation: `docs/architecture/00-index/agent-guardrails.md`.
- Multi-agent coordination: `docs/AGENT-PROCESS.md`.
- Per-tool entry point: `CLAUDE.md` (Claude Code), `AGENTS.md` (everyone else), `GEMINI.md` (Gemini CLI), `.cursor/rules/musubi.mdc` (Cursor).

Skills implement *workflows*, not *rules*. If a skill's output would contradict the guardrails, the guardrails win.
