<%*
const slug = await tp.system.prompt("Short slug (e.g. 'splade-vs-bm25')");
const title = await tp.system.prompt("Question (framed as a question)");
if (slug) await tp.file.rename(slug);
-%>
---
title: <% title || slug %>
section: _inbox
type: research-question
status: research-needed
priority: medium
blocks: []
tags: [type/research-question, status/research-needed]
updated: <% tp.date.now("YYYY-MM-DD") %>
---

# <% title || slug %>

## Question

> Frame the question precisely — one sentence.

## Why it matters

What decision or implementation is blocked until this is answered?

## What we think we know

## What we need to find out

- [R] Subquestion 1
- [R] Subquestion 2

## Prior art / candidate sources

- [ ] Paper / blog / benchmark.

## Resolution

When answered, summarise here and link to the spec(s) / ADR(s) that absorbed the answer. Then move this file to `_inbox/research/_resolved/` or convert into an ADR.

## Related

- [[00-index/research-questions]]
