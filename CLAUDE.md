# Musubi: Claude Code entry point

The contract for every coding agent and contributor on this repo is **[AGENTS.md](AGENTS.md)**. Read
it top to bottom before any edit; it is the only copy of the rules, so this file stays a pointer.

Claude Code specifics:

- Sub-agents in `.claude/agents/`: `musubi-reviewer` (independent PR review) and `musubi-spec-author`
  (writing or revising specs and ADRs).
- Skills in `.claude/skills/`: `spec-check` (docs health before a PR that touches `docs/Musubi/`).
- Operator-only hosts and credential pointers live in `.agent-context.local.md` at the repo root (not in
  git). Don't touch infrastructure (SSH, Ansible, model pulls) without it.
