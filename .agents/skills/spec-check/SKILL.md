---
name: spec-check
description: Run Musubi's docs health check (frontmatter, spec Test Contracts, wikilinks) and summarise it. Use before opening a PR that touches `docs/Musubi/`, or as a quick health check of the architecture docs.
---

# Skill: spec-check

Run the docs health check CI runs and give a one-screen report.

```bash
make agent-check        # = uv run python3 docs/Musubi/_tools/check.py all
```

Read the output carefully:

- `✗` lines are **errors** and block the PR in CI (broken frontmatter, a broken wikilink, a malformed
  spec).
- `⚠` lines are **warnings** (a complete spec with no Test Contract, a title mismatch). They don't
  block, but mention them.
- If the command exits non-zero, find the `✗` lines first; don't assume a pre-existing warning is the
  cause.

Report: error count and the files involved, warning count, and for each error the one-line fix
(usually a wikilink target that moved, or a missing frontmatter field). Don't edit files unless you
were asked to fix them.
