<%*
const title = await tp.system.prompt("Stub title", tp.file.title);
if (title) await tp.file.rename(title);
const folder = tp.file.folder(true).split("/").pop();
const section_tag = (folder.split("-", 2)[1] || folder);
-%>
---
title: <% title %>
section: <% folder %>
type: spec
status: stub
tags: [section/<% section_tag %>, status/stub, type/spec]
updated: <% tp.date.now("YYYY-MM-DD") %>
---

# <% title %>

> Placeholder. Expand when the upstream dependency is resolved or the research question answered.

## Open questions

- [R] What must be decided before this can be written?

## Links

- [[00-index/stubs]]
