---
title: Musubi Architecture Vault
status: living-document
type: vault-readme
vault-root: true
maintainer: ericmey@gmail.com
last-reviewed: 2026-04-17
updated: 2026-04-17
tags: [type/vault-readme, status/living-document]
reviewed: false
---
# Musubi Architecture Vault

This is the architectural specification for **Musubi (結び)** — the shared memory and knowledge plane for a small-team AI agent fleet. It is designed to be opened as an Obsidian vault.

Open `musubi/docs/architecture/` as a vault in Obsidian. Start at [[00-index/index|Root Index]].

## What this vault is

A hybrid **architecture + test-driven design specification**, grounded in April 2026 capabilities, deep enough to hand to a fleet of coding agents (or human engineers) with clear guardrails so they can ship independent slices in parallel without stepping on each other.

Every document in this vault is either a **specification** (the target system), a **test contract** (what success looks like), a **decision record** (why we chose it), or an **operational playbook** (how to run it). See [[13-decisions/index|Decisions]] for the trail of reasoning, and [[00-index/agent-guardrails|Agent Guardrails]] for the rules a coding agent must follow when working in this repo.

## What this vault is *not*

- Not a reflection of the current Musubi POC. See [[02-current-state/index|Current State]] for an honest gap analysis.
- Not a product roadmap. See [[12-roadmap/index|Roadmap]] for sequencing.
- Not a marketing document. Nothing here is aspirational — every claim is either implementable or flagged as a research question.

## Orientation

| If you are… | Start here |
|---|---|
| Opening the vault for the first time | [[00-index/dashboard]] |
| A coding agent picking up work | [[00-index/agent-guardrails]] → [[12-roadmap/phased-plan]] → your assigned slice |
| A human reviewer | [[00-index/executive-summary]] |
| Doing deployment | [[08-deployment/index]] |
| Implementing a retrieval path | [[05-retrieval/scoring-model]] |
| Writing a new adapter | [[07-interfaces/canonical-api]] → [[07-interfaces/sdk]] |
| Debugging in production | [[09-operations/runbooks]] |
| Looking for what's unfinished | [[00-index/research-questions]] or [[_bases/research-stubs]] |

## Vault layout

```
00-index/           navigation, glossary, conventions, dashboards
01-overview/        mission, personas, three planes
02-current-state/   POC inventory + gap analysis
03-system-design/   components, topology, failure modes
04-data-model/      object schemas + lifecycle
05-retrieval/       scoring, hybrid, fast/deep
06-ingestion/       capture, maturation, synthesis, promotion, vault-sync
07-interfaces/      canonical API, SDK, adapters
08-deployment/      Ansible, Docker Compose, GPU topology
09-operations/      runbooks, alerts, capacity, backup-restore
10-security/        auth, redaction, audit, data-handling
11-migration/       POC → v1 phases
12-roadmap/         v1/v2/v3 direction
13-decisions/       ADRs
_templates/         Templater templates for each note type
_bases/             Obsidian Bases (dynamic status / gap views)
_inbox/             research questions, locks, cross-slice tickets
_attachments/       images / binaries (excluded from the graph)
```

See [[00-index/conventions]] for the full schema.

## How gaps are tracked

Every note carries a `status:` frontmatter field — `complete`, `draft`, `stub`,
or `research-needed`. Browse the live views:

- [[_bases/by-status]] — every note, sortable by status.
- [[_bases/research-stubs]] — only stubs and research-needed notes.
- [[00-index/research-questions]] — narrative of the open questions.
- [[00-index/dashboard]] — at-a-glance section health.

The graph view is colour-coded by status (see `.obsidian/graph.json`) — open
the graph and research-needed nodes glow red; complete nodes are green. Open
orphan nodes in the graph to find unlinked notes.

## Obsidian plugins

### Installed (this vault is tuned for them)

- **Templater** — scaffolds new notes per folder. Try *Cmd+P → Templater: Create new note from template*.
- **Linter** — runs on save. Normalises frontmatter key order, deduplicates tags, trims trailing whitespace. Configured in `.obsidian/plugins/obsidian-linter/data.json`.
- **Tasks** — custom statuses include `[R]` (Research) and `[/]` (In Progress). Query blocks sit inside dashboards.
- **Dataview** — live tables/lists over frontmatter. Powers [[00-index/dashboard]], [[00-index/research-questions]], [[00-index/stubs]], and the per-section indexes. DataviewJS is enabled.
- **Breadcrumbs** — turns `up:` / `next:` / `prev:` / `depends-on:` / `blocks:` / `supersedes:` / `superseded-by:` frontmatter into an explicit graph. Open the matrix or trail view from the command palette.
- **Kanban** — boards at [[12-roadmap/slice-board]], [[11-migration/migration-board]], [[_inbox/research/research-board]].
- **Local REST API** — headless access from Musubi's own vault-sync pipeline.
- **Style Settings** — surface for tuning the `musubi-status-colors` CSS snippet.
- **Git** — auto-commit / push integration; open *Settings → Git* to enable.

### Built-in, enabled and configured

- **Bases** — spreadsheet views over frontmatter. Our dashboards live in `_bases/`.
- **Graph view** — colour-groups wired to status/type tags.
- **Backlinks / Outgoing / Properties / Canvas** — standard.

### Recommended extras

- **[Advanced Slides](https://obsidian.md/plugins?id=advanced-slides)** — if you ever want to present sections of this vault without leaving Obsidian.
- **[Iconize](https://obsidian.md/plugins?id=obsidian-icon-folder)** — per-folder icons; optional polish.
- **[Admonition](https://obsidian.md/plugins?id=obsidian-admonition)** — callout variants beyond the built-in set.

## Settings cheat-sheet

Key vault config (verify in *Settings → Files & links*):

- **New link format:** `Absolute path in vault` (so cross-section links don't silently break on rename).
- **Automatically update internal links:** ON.
- **Use `[[Wikilinks]]`:** ON.
- **Default location for new notes:** `Same folder as current file`.
- **Default location for new attachments:** `In the folder specified: _attachments`.
- **Strict line breaks:** OFF (respect authored wrap).
- **Readable line length:** ON.
- **Show frontmatter:** ON.

## Authoring a new note

1. Right-click the target folder → *New note from template* → pick the matching template (`_templates/spec.md`, `_templates/adr.md`, etc.).
2. Fill in the prompts (title, for ADRs the number).
3. Link it from the section's `index.md`.
4. Set `status:` to `draft` until the Open-questions section is empty.
5. Flip to `complete` when its Test Contract is covered (for specs) or when its decision is *accepted* (for ADRs).
