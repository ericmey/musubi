# GEMINI.md — entry point for Gemini CLI agents

Gemini CLI looks for `GEMINI.md` at the repo root. See `CLAUDE.md` (same directory) for the canonical guide — this file is a short pointer so content doesn't drift across agent tools.

**Must-reads before any coding work:**

1. [CLAUDE.md](./CLAUDE.md) — the full working agreement.
2. [AGENTS.md](./AGENTS.md) — the condensed version that applies to every coding agent.
3. [docs/AGENT-PROCESS.md](./docs/AGENT-PROCESS.md) — multi-agent concurrency model (GitHub Issues as the lock board).
4. [docs/architecture/00-index/agent-guardrails.md](./docs/architecture/00-index/agent-guardrails.md) — the four non-negotiables.

**Project conventions that apply to you:**

- Architecture specs: `docs/architecture/` (the Obsidian vault, in-repo).
- Slice registry: `docs/architecture/_slices/slice-<id>.md` — one coding task per file.
- Tests: mirror `src/musubi/` paths exactly under `tests/`.
- Language: Python 3.12, strict mypy, ruff format + lint, pydantic v2.
- Style: Conventional Commits, `spec-update:` trailer for same-PR spec changes.

**How Gemini should identify itself in the work log:**

When you append to a slice's `## Work log` section, use an agent identifier that starts with the model family — e.g., `gemini-3-1` or `gemini-3-1-pro`. This helps humans and other agents see at a glance which sessions came from which tool.

**Differences in capability:** Gemini's long context is useful for whole-vault reasoning (finding cross-slice impact of an ADR, for example). Prefer Gemini for spec revision + architecture review; prefer Claude Code for slice implementation. See [docs/AGENT-PROCESS.md](./docs/AGENT-PROCESS.md#agent-selection) for the recommended routing table.
