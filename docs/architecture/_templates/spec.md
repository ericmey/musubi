<%*
// Module spec template — use when creating a new spec note in 03–08, 10.
// Prompts for a title; auto-fills section from folder; defaults status to `draft`.
const title = await tp.system.prompt("Spec title", tp.file.title);
if (title) await tp.file.rename(title);
const folder = tp.file.folder(true).split("/").pop();
-%>
---
title: <% title %>
section: <% folder %>
type: spec
status: draft
owner:
tags: [section/<% folder.split("-", 2)[1] || folder %>, status/draft, type/spec]
updated: <% tp.date.now("YYYY-MM-DD") %>
depends-on: []
---

# <% title %>

## Purpose

> One sentence. What does this module do, and why is it load-bearing?

## Context

## Design

## Schema / Interface

```python
# pydantic models, function signatures, or HTTP routes
```

## Invariants

- [ ] Invariant 1
- [ ] Invariant 2

## Open questions

- [ ] Question — why it matters.

## Test Contract

Behaviors that must be covered before this module can be marked `status: complete`:

- [ ] Test 1 — given X, when Y, then Z.

## References

- [[13-decisions/]]
