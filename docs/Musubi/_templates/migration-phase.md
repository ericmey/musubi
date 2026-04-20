<%*
const title = await tp.system.prompt("Phase title, e.g. 'Phase 9: Something'", tp.file.title);
if (title) await tp.file.rename(title.toLowerCase().replace(/\s+/g, "-"));
-%>
---
title: <% title %>
section: 11-migration
type: migration-phase
status: draft
tags: [section/migration, status/draft, type/migration-phase]
updated: <% tp.date.now("YYYY-MM-DD") %>
depends-on: []
blocks: []
---

# <% title %>

## Goal

> One sentence. What state is the system in after this phase?

## Preconditions

- [ ] Previous phase complete.
- [ ] ...

## Steps

1. Step.
2. Step.

## Validation

- [ ] Contract tests pass.
- [ ] Rollback drill run.

## Risk & rollback

Describe the reversibility class and the exact rollback commands.

## Links

- [[11-migration/index]]
