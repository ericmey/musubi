---
title: "Question: Where should the MCP adapter live?"
section: _inbox
type: question
status: proposed
tags: [section/inbox, status/proposed, type/question, adapter, mcp]
updated: 2026-04-19
---

# Question: Where should the MCP adapter live?

**Goal:** Implement the MCP adapter as part of `slice-adapter-mcp`.

**Expectation:** The specifications provide a consistent location for the adapter code, allowing my agent to safely write to the `owns_paths` without violating architectural decisions.

**Observation:** There is a three-way contradiction in the documentation regarding the path for the MCP adapter:
1. `docs/architecture/_slices/slice-adapter-mcp.md` specifies `musubi-mcp/` as the owned path.
2. `docs/architecture/07-interfaces/mcp-adapter.md` specifies the module under test as `musubi-mcp-adapter/src/*` and describes it as an "Independent project. Repo: musubi-mcp-adapter".
3. `docs/architecture/13-decisions/0015-monorepo-supersedes-multi-repo.md` explicitly overrides the independent repo decision and mandates that it live at `src/musubi/adapters/mcp/`.

**Options:**
1. The operator updates the `owns_paths` in `docs/architecture/_slices/slice-adapter-mcp.md` to `src/musubi/adapters/mcp/` to align with the accepted monorepo ADR.
2. The operator clarifies that the MCP adapter should remain an independent workspace at the root level (`musubi-mcp/` or `musubi-mcp-adapter/`) and updates the ADR or slice file accordingly.