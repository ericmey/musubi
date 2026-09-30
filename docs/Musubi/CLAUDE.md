---
title: "Musubi — Coding Agent Entry Point"
type: vault-readme
status: living-document
tags: [type/vault-readme, status/living-document, agents]
updated: 2026-09-30
reviewed: true
---

# Musubi docs: agent entry point

The contract for every coding agent and contributor is **AGENTS.md** at the repo root (outside this
vault). It covers the non-negotiables, the issue-and-PR workflow, the Test Contract Closure Rule, review
and the prohibited patterns. This page only adds what's specific to the docs themselves.

- **Specs are the source of truth for behaviour.** Each area folder (`04-data-model/`, `05-retrieval/`, …)
  has specs with a `## Test Contract`, plus a section `CLAUDE.md` with local rules. Read both before
  changing code in that area.
- **Spec changes ride with the code.** Update the spec in the same PR, with a `spec-update: <doc-path>`
  commit trailer.
- **Docs health:** `make agent-check` (frontmatter, spec Test Contracts, wikilinks; see
  [[_tools/README]]).
- **Where things are:** [[00-index/index]] (vault index), [[00-index/conventions]] (style, frontmatter,
  tags), [[00-index/definition-of-done]], [[00-index/glossary]], [[13-decisions/index]] (ADRs).

The slice workflow that built v1.0 (`_slices/`, `_inbox/` locks and tickets) was retired on 2026-09-30;
its history is in git.
