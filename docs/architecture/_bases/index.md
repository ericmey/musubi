---
title: "Bases — Dynamic Views"
section: _bases
type: index
status: complete
tags: [type/index, status/complete]
updated: 2026-04-17
---

# Bases — Dynamic Views

Obsidian *Bases* treat the vault's frontmatter as a database. Open any `.base` file here as a read-only spreadsheet-style view over the notes it filters.

## Available bases

- [[_bases/by-status]] — every note, grouped by `status` (complete / draft / stub / research-needed). **Use this as the main gap dashboard.**
- [[_bases/research-stubs]] — only notes with `status: research-needed` or `status: stub`. These are the edges of the map.
- [[_bases/drafts]] — notes with `status: draft`. Needs follow-through.
- [[_bases/adrs]] — ADRs with status + supersession chain.
- [[_bases/migration-phases]] — phase-by-phase status of the POC → v1 migration.
- [[_bases/test-contracts]] — every spec with a Test Contract section. Coverage index.
- [[_bases/section-status]] — notes grouped by section number.

## How to extend

1. Copy one of the existing `.base` files.
2. Edit the `filters:` block — any frontmatter property is queryable as `note.<key>`.
3. Edit `views:` to add table/card/calendar layouts.

## Related

- [[00-index/dashboard]] — narrative dashboard with ASCII tables.
- [[00-index/research-questions]] — the open research pipeline.
- [[00-index/conventions]] — frontmatter schema.
