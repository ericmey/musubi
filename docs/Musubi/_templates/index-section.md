<%*
const title = await tp.system.prompt("Section title, e.g. 'Operations'");
const folder = tp.file.folder(true).split("/").pop();
-%>
---
title: <% title %>
section: <% folder %>
type: index
status: complete
tags: [section/<% folder.split("-", 2)[1] %>, status/complete, type/index]
updated: <% tp.date.now("YYYY-MM-DD") %>
---

# <% folder %> — <% title %>

## Purpose

What this section exists to answer.

## Documents in this section

- [[<% folder %>/...]] — ...

## Cross-section links

- Inbound: other sections that reference this one.
- Outbound: other sections this one depends on.

## Status

See [[_bases/by-status]] filtered to this section.
