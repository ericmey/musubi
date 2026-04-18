<%*
const title = await tp.system.prompt("Runbook title (alert name or operator action)", tp.file.title);
if (title) await tp.file.rename(title);
-%>
---
title: <% title %>
section: 09-operations
type: runbook
status: draft
owner: oncall
tags: [section/operations, status/draft, type/runbook]
updated: <% tp.date.now("YYYY-MM-DD") %>
alerts: []
---

# <% title %>

## Alert / Trigger

**Alert name:** `<alert_name>`
**Fires when:** <condition>
**Urgency:** p1 | p2 | p3

## Quick diagnosis

1. Check X.
2. Check Y.

## Recovery steps

1. Step 1.
2. Step 2.

## Verify healed

- [ ] Metric M returned to baseline.
- [ ] Alert cleared.

## Postmortem trigger

If recovery takes > N minutes or recurs, file incident in [[09-operations/runbooks]].

## References

- [[09-operations/alerts]]
- [[09-operations/observability]]
