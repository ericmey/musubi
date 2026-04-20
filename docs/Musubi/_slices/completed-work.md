---
title: Completed Slices
section: _slices
type: index
status: complete
tags: [section/slices, status/complete, type/index]
updated: 2026-04-17
up: "[[_slices/index]]"
reviewed: true
---

# Completed Slices

Pure Dataview over `status: done` slices — no manual history to maintain. The slice's own note is the history; this page aggregates them chronologically.

## Shipped, newest first

```dataview
TABLE WITHOUT ID
  file.link AS "Slice",
  phase AS "Phase",
  owner AS "Shipped by",
  updated AS "Shipped on"
FROM "_slices"
WHERE type = "slice" AND status = "done"
SORT updated DESC
```

## Phase completion

```dataview
TABLE WITHOUT ID
  phase AS "Phase",
  length(rows) AS "total",
  length(filter(rows, (r) => r.status = "done")) AS "✅ shipped",
  length(filter(rows, (r) => r.status = "in-progress" OR r.status = "in-review")) AS "🚧 in flight",
  length(filter(rows, (r) => r.status = "blocked")) AS "🛑 blocked",
  length(filter(rows, (r) => r.status = "ready")) AS "⏳ ready"
FROM "_slices"
WHERE type = "slice"
GROUP BY phase
SORT phase ASC
```

## Who shipped what

```dataview
TABLE WITHOUT ID
  owner AS "Agent",
  length(rows) AS "shipped",
  length(filter(rows, (r) => r.status = "done")) AS "✅"
FROM "_slices"
WHERE type = "slice" AND owner != "unassigned"
GROUP BY owner
SORT length(rows) DESC
```

## Takeaways (appended by hand, only when non-obvious)

Agents append lessons *that will change how future slices are done*. Don't recap — extract.

- _(example)_ `slice-embedding`: TEI's health endpoint lies on cold boot for ~20s. Add a `pytest-docker` wait-for-embedding fixture; bootstrap tests assume it's ready. → Applied to [[_slices/test-fixtures]].

Format:

```
- **slice-id**: one-line lesson. → Where it was applied (spec, fixture, convention).
```
