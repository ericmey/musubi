<%*
const num = await tp.system.prompt("ADR number (4 digits, e.g. 0013)");
const title = await tp.system.prompt("ADR title (short, decision-shaped)");
if (num && title) await tp.file.rename(`${num}-${title.toLowerCase().replace(/\s+/g, "-")}`);
-%>
---
title: "ADR <% num %>: <% title %>"
section: 13-decisions
type: adr
status: proposed
date: <% tp.date.now("YYYY-MM-DD") %>
deciders: [Eric]
tags: [section/decisions, status/proposed, type/adr]
updated: <% tp.date.now("YYYY-MM-DD") %>
supersedes:
superseded-by:
---

# ADR <% num %>: <% title %>

**Status:** proposed
**Date:** <% tp.date.now("YYYY-MM-DD") %>
**Deciders:** Eric

## Context

## Decision

## Consequences

### Positive

### Negative

### Neutral

## Alternatives considered

### Alternative A

- Why considered.
- Why rejected.

## References

- [[13-decisions/sources]]
